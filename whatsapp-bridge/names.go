package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// Name sources, best first. A chat whose name came from "fallback" (a bare
// number) is re-resolved whenever new contact data arrives.
const (
	srcGroup    = "group"
	srcContact  = "contact"
	srcBusiness = "business"
	srcPush     = "push"
	srcFallback = "fallback"
)

// migrateNames adds name columns and the contacts table to messages.db.
// Additive only: existing rows and columns are untouched.
func migrateNames(db *sql.DB) error {
	if _, err := db.Exec(`ALTER TABLE chats ADD COLUMN name_source TEXT`); err != nil &&
		!strings.Contains(err.Error(), "duplicate column") {
		return fmt.Errorf("add chats.name_source: %w", err)
	}
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS contacts (
			user          TEXT PRIMARY KEY, -- JID user part: phone digits or LID digits
			jid           TEXT NOT NULL,
			phone         TEXT,             -- phone digits, when known
			lid           TEXT,             -- LID digits, when known
			name          TEXT NOT NULL,    -- best display name
			name_source   TEXT NOT NULL,
			full_name     TEXT,
			first_name    TEXT,
			push_name     TEXT,
			business_name TEXT,
			updated_at    TIMESTAMP
		);
		CREATE INDEX IF NOT EXISTS idx_contacts_phone ON contacts(phone);
		CREATE INDEX IF NOT EXISTS idx_contacts_lid ON contacts(lid);
	`)
	return err
}

// ResolvedContact is a person with both of their identities and a display name.
type ResolvedContact struct {
	Phone, LID   types.JID
	Name, Source string
	Info         types.ContactInfo
}

// NameResolver maps JIDs to human names using whatsmeow's LID map and contact
// store, and keeps messages.db chats and contacts in sync.
type NameResolver struct {
	client *whatsmeow.Client
	db     *sql.DB
	log    waLog.Logger
	mu     sync.Mutex // serializes backfills
	once   sync.Once  // one automatic backfill per process
}

func NewNameResolver(client *whatsmeow.Client, db *sql.DB, log waLog.Logger) *NameResolver {
	return &NameResolver{client: client, db: db, log: log}
}

// lookups abstracts where LID mappings and contact info come from: whatsmeow's
// stores for single lookups, in-memory snapshots for the bulk backfill.
type lookups struct {
	pnForLID func(types.JID) types.JID
	lidForPN func(types.JID) types.JID
	contact  func(types.JID) (types.ContactInfo, bool)
}

func (r *NameResolver) storeLookups(ctx context.Context) lookups {
	return lookups{
		pnForLID: func(j types.JID) types.JID { pn, _ := r.client.Store.LIDs.GetPNForLID(ctx, j); return pn },
		lidForPN: func(j types.JID) types.JID { l, _ := r.client.Store.LIDs.GetLIDForPN(ctx, j); return l },
		contact: func(j types.JID) (types.ContactInfo, bool) {
			info, err := r.client.Store.Contacts.GetContact(ctx, j)
			return info, err == nil && info.Found
		},
	}
}

// ResolveUser resolves a person given either their phone JID or their LID.
func (r *NameResolver) ResolveUser(ctx context.Context, jid types.JID) ResolvedContact {
	return resolveWith(r.storeLookups(ctx), jid)
}

func resolveWith(l lookups, jid types.JID) ResolvedContact {
	jid = jid.ToNonAD()
	var rc ResolvedContact
	switch jid.Server {
	case types.HiddenUserServer:
		rc.LID = jid
		rc.Phone = l.pnForLID(jid)
	case types.DefaultUserServer:
		rc.Phone = jid
		rc.LID = l.lidForPN(jid)
	default:
		rc.Phone = jid
	}

	for _, j := range []types.JID{rc.Phone, rc.LID} {
		if j.IsEmpty() {
			continue
		}
		if info, ok := l.contact(j); ok {
			mergeInfo(&rc.Info, info)
		}
	}

	switch {
	case rc.Info.FullName != "":
		rc.Name, rc.Source = rc.Info.FullName, srcContact
	case rc.Info.FirstName != "":
		rc.Name, rc.Source = rc.Info.FirstName, srcContact
	case rc.Info.BusinessName != "":
		rc.Name, rc.Source = rc.Info.BusinessName, srcBusiness
	case rc.Info.PushName != "" && rc.Info.PushName != "-":
		rc.Name, rc.Source = rc.Info.PushName, srcPush
	case !rc.Phone.IsEmpty() && rc.Phone.Server == types.DefaultUserServer:
		rc.Name, rc.Source = "+"+rc.Phone.User, srcFallback
	default:
		rc.Name, rc.Source = jid.User, srcFallback
	}
	return rc
}

func mergeInfo(dst *types.ContactInfo, src types.ContactInfo) {
	if dst.FullName == "" {
		dst.FullName = src.FullName
	}
	if dst.FirstName == "" {
		dst.FirstName = src.FirstName
	}
	if dst.PushName == "" {
		dst.PushName = src.PushName
	}
	if dst.BusinessName == "" {
		dst.BusinessName = src.BusinessName
	}
}

// execer is satisfied by *sql.DB and *sql.Tx.
type execer interface {
	Exec(query string, args ...any) (sql.Result, error)
}

// saveContact writes one row per identity (phone and LID) so a lookup by either
// user part finds the name, and renames that person's DM chats.
func (r *NameResolver) saveContact(e execer, rc ResolvedContact) error {
	phone, lid := rc.Phone.User, rc.LID.User
	if rc.Phone.Server != types.DefaultUserServer {
		phone = ""
	}
	now := time.Now()
	for _, j := range []types.JID{rc.Phone, rc.LID} {
		if j.IsEmpty() {
			continue
		}
		_, err := e.Exec(`
			INSERT INTO contacts (user, jid, phone, lid, name, name_source, full_name, first_name, push_name, business_name, updated_at)
			VALUES (?, ?, NULLIF(?, ''), NULLIF(?, ''), ?, ?, NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), ?)
			ON CONFLICT(user) DO UPDATE SET
				jid = excluded.jid,
				phone = COALESCE(excluded.phone, contacts.phone),
				lid = COALESCE(excluded.lid, contacts.lid),
				name = CASE WHEN excluded.name_source = 'fallback' AND contacts.name_source != 'fallback'
				            THEN contacts.name ELSE excluded.name END,
				name_source = CASE WHEN excluded.name_source = 'fallback' AND contacts.name_source != 'fallback'
				            THEN contacts.name_source ELSE excluded.name_source END,
				full_name = COALESCE(excluded.full_name, contacts.full_name),
				first_name = COALESCE(excluded.first_name, contacts.first_name),
				push_name = COALESCE(excluded.push_name, contacts.push_name),
				business_name = COALESCE(excluded.business_name, contacts.business_name),
				updated_at = excluded.updated_at`,
			j.User, j.String(), phone, lid, rc.Name, rc.Source,
			rc.Info.FullName, rc.Info.FirstName, rc.Info.PushName, rc.Info.BusinessName, now)
		if err != nil {
			return err
		}
		if err := setChatName(e, j.String(), rc.Name, rc.Source); err != nil {
			return err
		}
	}
	return nil
}

// setChatName renames an existing chat unless that would replace a real name
// with a bare number.
func setChatName(e execer, chatJID, name, source string) error {
	_, err := e.Exec(`
		UPDATE chats SET name = ?, name_source = ?
		WHERE jid = ? AND NOT (? = 'fallback' AND COALESCE(name_source, 'fallback') != 'fallback')`,
		name, source, chatJID, source)
	return err
}

// ChatName returns the display name for a chat JID and where it came from.
// Groups use the stored name unless it is a placeholder, then group metadata.
func (r *NameResolver) ChatName(ctx context.Context, jid types.JID, historyName string) (string, string) {
	if jid.Server == types.GroupServer {
		if historyName != "" {
			return historyName, srcGroup
		}
		var name, source sql.NullString
		_ = r.db.QueryRow(`SELECT name, name_source FROM chats WHERE jid = ?`, jid.String()).Scan(&name, &source)
		if name.String != "" && source.String == srcGroup {
			return name.String, srcGroup
		}
		if info, err := r.client.GetGroupInfo(ctx, jid); err == nil && info.Name != "" {
			return info.Name, srcGroup
		}
		if name.String != "" && !strings.HasPrefix(name.String, "Group ") {
			return name.String, srcGroup
		}
		return "Group " + jid.User, srcFallback
	}
	if jid.Server == types.DefaultUserServer || jid.Server == types.HiddenUserServer {
		rc := r.ResolveUser(ctx, jid)
		return rc.Name, rc.Source
	}
	return jid.User, srcFallback
}

// Backfill names every contact, every DM chat and sender, and every joined
// group. Safe to run repeatedly.
func (r *NameResolver) Backfill(ctx context.Context) (map[string]int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	start := time.Now()
	stats := map[string]int{}

	users := map[types.JID]struct{}{}
	all, err := r.client.Store.Contacts.GetAllContacts(ctx)
	if err != nil {
		return nil, fmt.Errorf("load contacts: %w", err)
	}
	for j := range all {
		users[j.ToNonAD()] = struct{}{}
	}
	l, err := r.snapshotLookups(all)
	if err != nil {
		return nil, err
	}
	rows, err := r.db.Query(`
		SELECT jid FROM chats WHERE jid LIKE '%@s.whatsapp.net' OR jid LIKE '%@lid'
		UNION SELECT DISTINCT sender FROM messages WHERE sender != ''`)
	if err != nil {
		return nil, fmt.Errorf("load chats/senders: %w", err)
	}
	// Read everything before doing lookups: an open cursor blocks message writes.
	raw := scanStrings(rows)
	for _, s := range raw {
		if j, ok := parseUser(l, s); ok {
			users[j] = struct{}{}
		}
	}
	r.log.Infof("Name backfill: resolving %d identities", len(users))

	// Resolve first, then write in one short transaction so message storage
	// isn't blocked behind the slow lookups.
	resolved := make([]ResolvedContact, 0, len(users))
	for j := range users {
		rc := resolveWith(l, j)
		resolved = append(resolved, rc)
		stats["contacts"]++
		if rc.Source != srcFallback {
			stats["named"]++
		}
	}
	if err := r.saveAll(resolved); err != nil {
		return nil, err
	}
	if r.client.IsConnected() {
		stats["verified"] = r.backfillVerifiedNames(ctx, l)
	}

	if r.client.IsConnected() {
		groups, err := r.client.GetJoinedGroups(ctx)
		if err != nil {
			r.log.Warnf("Name backfill: could not list groups: %v", err)
		}
		for _, g := range groups {
			if g.Name == "" {
				continue
			}
			if res, err := r.db.Exec(`UPDATE chats SET name = ?, name_source = ? WHERE jid = ?`,
				g.Name, srcGroup, g.JID.String()); err == nil {
				if n, _ := res.RowsAffected(); n > 0 {
					stats["groups"]++
				}
			}
		}
	}
	r.log.Infof("Name backfill done in %s: %v", time.Since(start).Round(time.Millisecond), stats)
	return stats, nil
}

// saveAll writes in small transactions so live message writes only ever wait
// for one batch.
func (r *NameResolver) saveAll(contacts []ResolvedContact) error {
	const batch = 200
	for i := 0; i < len(contacts); i += batch {
		tx, err := r.db.Begin()
		if err != nil {
			return err
		}
		for _, rc := range contacts[i:min(i+batch, len(contacts))] {
			if err := r.saveContact(tx, rc); err != nil {
				tx.Rollback()
				return fmt.Errorf("save %s: %w", rc.Name, err)
			}
		}
		if err := tx.Commit(); err != nil {
			return err
		}
		time.Sleep(20 * time.Millisecond) // let queued message writes in
	}
	return nil
}

// StartBackgroundBackfill runs one backfill per process, a while after the
// first connect so it doesn't compete with the offline-message catch-up.
func (r *NameResolver) StartBackgroundBackfill(delay time.Duration) {
	r.once.Do(func() {
		go func() {
			time.Sleep(delay)
			if _, err := r.Backfill(context.Background()); err != nil {
				r.log.Warnf("Name backfill failed: %v", err)
			}
		}()
	})
}

// backfillVerifiedNames asks WhatsApp for verified business names of DM chats
// active in the last 90 days that still only have a number. Returns how many
// were named.
func (r *NameResolver) backfillVerifiedNames(ctx context.Context, l lookups) int {
	rows, err := r.db.Query(`
		SELECT jid FROM chats
		WHERE name_source = 'fallback' AND (jid LIKE '%@s.whatsapp.net' OR jid LIKE '%@lid')
		  AND last_message_time > ?`, time.Now().AddDate(0, 0, -90))
	if err != nil {
		return 0
	}
	byPhone := map[types.JID]ResolvedContact{}
	for _, s := range scanStrings(rows) {
		if j, err := types.ParseJID(s); err == nil {
			if rc := resolveWith(l, j); !rc.Phone.IsEmpty() && rc.Phone.Server == types.DefaultUserServer {
				byPhone[rc.Phone] = rc
			}
		}
	}
	r.log.Infof("Name backfill: looking up verified names for %d numbers", len(byPhone))

	phones := make([]types.JID, 0, len(byPhone))
	for p := range byPhone {
		phones = append(phones, p)
	}
	var named []ResolvedContact
	for i := 0; i < len(phones); i += 50 {
		batch := phones[i:min(i+50, len(phones))]
		infos, err := r.client.GetUserInfo(ctx, batch)
		if err != nil {
			r.log.Warnf("Name backfill: user info lookup failed: %v", err)
			break
		}
		for p, info := range infos {
			if info.VerifiedName == nil || info.VerifiedName.Details.GetVerifiedName() == "" {
				continue
			}
			rc, ok := byPhone[p.ToNonAD()]
			if !ok {
				continue
			}
			rc.Name, rc.Source = info.VerifiedName.Details.GetVerifiedName(), srcBusiness
			rc.Info.BusinessName = rc.Name
			named = append(named, rc)
		}
	}
	if err := r.saveAll(named); err != nil {
		r.log.Warnf("Name backfill: saving verified names failed: %v", err)
		return 0
	}
	return len(named)
}

// snapshotLookups reads the whole LID map once (read-only) so the backfill
// does not issue thousands of queries against the live session database.
func (r *NameResolver) snapshotLookups(all map[types.JID]types.ContactInfo) (lookups, error) {
	db, err := sql.Open("sqlite3", "file:store/whatsapp.db?mode=ro&_busy_timeout=5000")
	if err != nil {
		return lookups{}, err
	}
	defer db.Close()
	rows, err := db.Query(`SELECT lid, pn FROM whatsmeow_lid_map`)
	if err != nil {
		return lookups{}, fmt.Errorf("read LID map: %w", err)
	}
	lidToPN, pnToLID := map[string]string{}, map[string]string{}
	for rows.Next() {
		var lid, pn string
		if rows.Scan(&lid, &pn) == nil {
			lidToPN[lid], pnToLID[pn] = pn, lid
		}
	}
	rows.Close()
	contacts := make(map[types.JID]types.ContactInfo, len(all))
	for j, info := range all {
		contacts[j.ToNonAD()] = info
	}
	return lookups{
		pnForLID: func(j types.JID) types.JID {
			if pn, ok := lidToPN[j.User]; ok {
				return types.NewJID(pn, types.DefaultUserServer)
			}
			return types.JID{}
		},
		lidForPN: func(j types.JID) types.JID {
			if lid, ok := pnToLID[j.User]; ok {
				return types.NewJID(lid, types.HiddenUserServer)
			}
			return types.JID{}
		},
		contact: func(j types.JID) (types.ContactInfo, bool) {
			info, ok := contacts[j]
			return info, ok
		},
	}, nil
}

// scanStrings reads a single-column result fully and closes it.
func scanStrings(rows *sql.Rows) []string {
	defer rows.Close()
	var out []string
	for rows.Next() {
		var s string
		if rows.Scan(&s) == nil {
			out = append(out, s)
		}
	}
	return out
}

// parseUser turns a stored sender or chat JID ("972…", "972…@s.whatsapp.net",
// "2403…@lid") into a user JID. Bare digits are ambiguous; a LID map hit wins.
func parseUser(l lookups, s string) (types.JID, bool) {
	if s == "" {
		return types.JID{}, false
	}
	if strings.Contains(s, "@") {
		j, err := types.ParseJID(s)
		if err != nil || (j.Server != types.DefaultUserServer && j.Server != types.HiddenUserServer) {
			return types.JID{}, false
		}
		return j.ToNonAD(), true
	}
	return resolveBareUser(l, s), true
}

// resolveBareUser picks the server for a bare user part stored by older code,
// which may be a LID or a phone number.
func resolveBareUser(l lookups, user string) types.JID {
	lid := types.NewJID(user, types.HiddenUserServer)
	if !l.pnForLID(lid).IsEmpty() {
		return lid
	}
	if len(user) >= 15 { // longer than real phone numbers in practice: an unmapped LID
		return lid
	}
	return types.NewJID(user, types.DefaultUserServer)
}

// HandleEvent keeps names fresh when contacts, push names or groups change.
func (r *NameResolver) HandleEvent(evt any) {
	ctx := context.Background()
	refresh := func(jids ...types.JID) {
		for _, j := range jids {
			if j.IsEmpty() {
				continue
			}
			if err := r.saveContact(r.db, r.ResolveUser(ctx, j)); err != nil {
				r.log.Warnf("Failed to update name for %s: %v", j, err)
			}
		}
	}
	switch v := evt.(type) {
	case *events.Contact:
		refresh(v.JID)
	case *events.PushName:
		refresh(v.JID, v.JIDAlt)
	case *events.BusinessName:
		refresh(v.JID)
	case *events.GroupInfo:
		if v.Name != nil && v.Name.Name != "" {
			_ = setChatName(r.db, v.JID.String(), v.Name.Name, srcGroup)
		}
	case *events.JoinedGroup:
		if v.GroupInfo.Name != "" {
			_ = setChatName(r.db, v.JID.String(), v.GroupInfo.Name, srcGroup)
		}
	case *events.Message:
		// Message senders carry push names; record unknown ones as they appear.
		if !v.Info.IsFromMe && !v.Info.Sender.IsEmpty() {
			refresh(v.Info.Sender)
		}
	}
}

func (r *NameResolver) registerHandler() {
	http.HandleFunc("/api/resync-names", func(w http.ResponseWriter, req *http.Request) {
		if req.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		stats, err := r.Backfill(req.Context())
		w.Header().Set("Content-Type", "application/json")
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]any{"success": false, "message": err.Error()})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"success": true, "stats": stats})
	})
}
