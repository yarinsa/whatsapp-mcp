package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// Unread reconciliation with the phone.
//
// WhatsApp has no API that returns the phone's badge or per-chat unread
// counts. What the phone does share is app state: markChatAsRead mutations
// (read / marked-unread plus the timestamp of the chat's last message at the
// time) and deleteChat mutations. SyncUnread re-reads that state, backfills
// only threads that look unread, and recounts them from stored messages. No
// read receipts are ever sent.

type phoneMarker struct {
	read   bool
	lastTS time.Time
}

type unreadSync struct {
	mu       sync.Mutex
	running  bool
	lastRun  time.Time
	result   map[string]any
	markers  map[string]phoneMarker // filled while a sync collects app-state events
	deleted  map[string]bool
	collect  bool
	phoneTot int
	waiters  map[string]chan *waHistorySync.Conversation
}

var unreadSyncer = &unreadSync{}

// observe records phone-side markers while a sync is collecting.
func (u *unreadSync) observe(evt any) {
	u.mu.Lock()
	defer u.mu.Unlock()
	if !u.collect {
		return
	}
	switch v := evt.(type) {
	case *events.MarkChatAsRead:
		ts := time.Unix(v.Action.GetMessageRange().GetLastMessageTimestamp(), 0)
		u.markers[v.JID.ToNonAD().String()] = phoneMarker{read: v.Action.GetRead(), lastTS: ts}
	case *events.DeleteChat:
		u.deleted[v.JID.ToNonAD().String()] = true
	}
}

func (rs *ReadState) storeUnreadTotal() int {
	var n int
	rs.db.QueryRow(`SELECT COALESCE(SUM(CASE WHEN unread_count > 0 THEN unread_count WHEN marked_unread THEN 1 ELSE 0 END), 0) FROM chats`).Scan(&n)
	return n
}

// answered hands an on-demand history answer to the sync waiting for it.
// The phone's answer carries the chat's own unread count when it has one,
// which beats anything we can count locally.
func (u *unreadSync) answered(c *waHistorySync.Conversation) {
	u.mu.Lock()
	ch := u.waiters[c.GetID()]
	u.mu.Unlock()
	if ch != nil {
		select {
		case ch <- c:
		default:
		}
	}
}

// UnreadState is reported by /api/health and /api/unread-state.
func (rs *ReadState) UnreadState() map[string]any {
	u := unreadSyncer
	u.mu.Lock()
	lastRun, phoneTot := u.lastRun, u.phoneTot
	u.mu.Unlock()
	health.mu.Lock()
	connectedAt := health.connectedAt
	health.mu.Unlock()
	store := rs.storeUnreadTotal()
	out := map[string]any{"store_unread_total": store}
	if lastRun.IsZero() {
		out["phone_unread_total"] = nil
		out["drift"] = true
		out["hint"] = "Unread state hasn't been reconciled with the phone yet: call sync_unread"
		return out
	}
	out["phone_unread_total"] = phoneTot
	out["last_unread_sync"] = lastRun.UTC().Format(time.RFC3339)
	drift := lastRun.Before(connectedAt)
	out["drift"] = drift
	if drift {
		out["hint"] = "The bridge reconnected since the last reconcile, so messages may be missing: call sync_unread"
	}
	return out
}

// SyncUnread reconciles unread state with the phone. maxChats caps backfills.
func (rs *ReadState) SyncUnread(ctx context.Context, maxChats int) (map[string]any, error) {
	u := unreadSyncer
	u.mu.Lock()
	if u.running {
		u.mu.Unlock()
		return nil, fmt.Errorf("a sync is already running")
	}
	u.running, u.collect = true, true
	u.markers, u.deleted = map[string]phoneMarker{}, map[string]bool{}
	u.mu.Unlock()
	defer func() { u.mu.Lock(); u.running, u.collect = false, false; u.mu.Unlock() }()

	if rs.client == nil || !rs.client.IsConnected() || rs.client.Store.ID == nil {
		return nil, fmt.Errorf("not connected to WhatsApp")
	}
	if maxChats <= 0 || maxChats > 50 {
		maxChats = 40
	}
	before := rs.storeUnreadTotal()

	// 1. Phone markers: a full app-state read of the patches that carry them.
	rs.client.EmitAppStateEventsOnFullSync = true
	for _, p := range []appstate.WAPatchName{appstate.WAPatchRegularLow, appstate.WAPatchRegularHigh} {
		if err := rs.client.FetchAppState(ctx, p, true, false); err != nil {
			rs.log.Warnf("sync_unread: app state %s: %v", p, err)
		}
	}
	u.mu.Lock()
	u.collect = false
	markers, deleted := u.markers, u.deleted
	u.mu.Unlock()

	fixed := map[string]string{}

	// 2. Stale entries: deleted chats, unfollowed channels, groups left.
	stale := map[string]string{}
	for j := range deleted {
		stale[j] = "deleted on the phone"
	}
	if subs, err := rs.client.GetSubscribedNewsletters(ctx); err == nil {
		followed := map[string]bool{}
		for _, n := range subs {
			followed[n.ID.String()] = true
		}
		for _, j := range rs.unreadJIDs(`jid LIKE '%@newsletter'`) {
			if !followed[j] {
				stale[j] = "channel no longer followed"
			}
		}
	}
	if groups, err := rs.client.GetJoinedGroups(ctx); err == nil {
		joined := map[string]bool{}
		for _, g := range groups {
			joined[g.JID.String()] = true
		}
		for _, j := range rs.unreadJIDs(`jid LIKE '%@g.us'`) {
			if !joined[j] {
				stale[j] = "group left"
			}
		}
	}
	for j, why := range stale {
		rs.setRead(j)
		fixed[j] = why
	}

	// 3. Apply phone markers. Read up to (or past) our newest message → read.
	// Marked unread → flag it, and backfill.
	type cand struct {
		jid   string
		count int
	}
	var cands []cand
	seen := map[string]bool{}
	for j, m := range markers {
		if stale[j] != "" {
			continue
		}
		newest, _ := rs.newestMessage(j)
		switch {
		case !m.read:
			rs.db.Exec(`UPDATE chats SET marked_unread = 1 WHERE jid = ? AND marked_unread = 0`, j)
			cands = append(cands, cand{j, 20})
			seen[j] = true
		case !m.lastTS.Before(newest):
			// Read on the phone at or after our newest message: never fetch.
			if res, _ := rs.db.Exec(`UPDATE chats SET unread_count = 0, marked_unread = 0 WHERE jid = ? AND (unread_count > 0 OR marked_unread)`, j); res != nil {
				if n, _ := res.RowsAffected(); n > 0 {
					fixed[j] = "read on the phone"
				}
			}
			seen[j] = true
		}
	}

	// 4. Other candidates: chats the store counts as unread, and chats active
	// during the last week whose newest stored message predates the last
	// outage (where a gap can hide). Chats the phone marked read are skipped.
	health.mu.Lock()
	since := health.connectedAt.Add(-7 * 24 * time.Hour)
	health.mu.Unlock()
	rows, err := rs.db.Query(`SELECT jid, unread_count FROM chats
		WHERE jid NOT LIKE '%@newsletter' AND jid NOT LIKE '%@broadcast' AND jid != 'status@broadcast'
		  AND (unread_count > 0 OR last_message_time >= ?)
		ORDER BY unread_count > 0 DESC, last_message_time DESC`, since)
	if err == nil {
		for rows.Next() {
			var j string
			var n int
			rows.Scan(&j, &n)
			if seen[j] || stale[j] != "" {
				continue
			}
			seen[j] = true
			cands = append(cands, cand{j, n + 5})
		}
		rows.Close()
	}
	if len(cands) > maxChats {
		cands = cands[:maxChats]
	}

	// 5. Backfill each candidate with the newest messages, rate-limited.
	backfilled := 0
	perChat := []map[string]any{}
	for i, c := range cands {
		if ctx.Err() != nil {
			break
		}
		if i > 0 {
			time.Sleep(500 * time.Millisecond)
		}
		count := min(c.count, 50)
		prevNewest, _ := rs.newestMessage(c.jid)
		n, conv, err := rs.fetchLatest(ctx, c.jid, count, 10*time.Second)
		entry := map[string]any{"jid": c.jid, "requested": count, "fetched": n}
		if err != nil {
			entry["error"] = err.Error()
		}
		backfilled += n
		var prevUnread int
		var prevMarked bool
		rs.db.QueryRow(`SELECT unread_count, marked_unread FROM chats WHERE jid = ?`, c.jid).Scan(&prevUnread, &prevMarked)
		switch {
		case conv != nil && conv.UnreadCount != nil:
			// The phone's own count for this chat.
			phone, marked := int(conv.GetUnreadCount()), conv.GetMarkedAsUnread()
			entry["phone_unread"] = phone
			if phone != prevUnread || marked != prevMarked {
				rs.ApplyHistory(c.jid, uint32(phone), marked)
				fixed[c.jid] = fmt.Sprintf("unread %d → %d (phone)", prevUnread, phone)
			}
		case n > 0:
			if added := rs.recountAfter(c.jid, prevNewest); added > 0 {
				fixed[c.jid] = fmt.Sprintf("backfilled %d messages, %d unread", n, added)
				entry["unread_added"] = added
			}
		}
		rs.log.Infof("sync_unread: %s requested %d, got %d new, phone unread %v, err %v", c.jid, count, n, entry["phone_unread"], err)
		perChat = append(perChat, entry)
	}

	after := rs.storeUnreadTotal()
	fixedList := []map[string]string{}
	for j, why := range fixed {
		fixedList = append(fixedList, map[string]string{"jid": j, "name": rs.chatName(j), "change": why})
	}
	res := map[string]any{
		"phone_markers_read":  len(markers),
		"chats_checked":       len(cands),
		"chats_fixed":         len(fixed),
		"messages_backfilled": backfilled,
		"store_unread_before": before,
		"store_unread_after":  after,
		"fixed":               fixedList,
		"backfill":            perChat,
		"note":                "WhatsApp doesn't expose the phone's badge count; the phone total is the store total after reconciling with the phone's read markers and backfilling likely-unread threads.",
	}
	u.mu.Lock()
	u.lastRun, u.phoneTot, u.result = time.Now(), after, res
	u.mu.Unlock()
	return res, nil
}

func (rs *ReadState) unreadJIDs(where string) []string {
	rows, err := rs.db.Query(`SELECT jid FROM chats WHERE (unread_count > 0 OR marked_unread) AND ` + where)
	if err != nil {
		return nil
	}
	return scanStrings(rows)
}

func (rs *ReadState) chatName(j string) string {
	var n sql.NullString
	rs.db.QueryRow(`SELECT name FROM chats WHERE jid = ?`, j).Scan(&n)
	return n.String
}

func (rs *ReadState) newestMessage(j string) (time.Time, bool) {
	var ts sql.NullTime
	rs.db.QueryRow(`SELECT timestamp FROM messages WHERE chat_jid = ? ORDER BY julianday(timestamp) DESC LIMIT 1`, j).Scan(&ts)
	return ts.Time, ts.Valid
}

// recountAfter adds incoming messages newer than prev (and newer than your
// own last reply) to the chat's unread count. Returns how many were added.
func (rs *ReadState) recountAfter(j string, prev time.Time) int {
	var n int
	rs.db.QueryRow(`SELECT COUNT(*) FROM messages WHERE chat_jid = ? AND is_from_me = 0
		AND julianday(timestamp) > julianday(?)
		AND julianday(timestamp) > COALESCE((SELECT MAX(julianday(timestamp)) FROM messages WHERE chat_jid = ? AND is_from_me = 1), 0)`,
		j, prev, j).Scan(&n)
	if n > 0 {
		rs.db.Exec(`UPDATE chats SET unread_count = unread_count + ? WHERE jid = ?`, n, j)
	}
	return n
}

// fetchLatest asks the phone for the newest count messages of a chat (an
// on-demand history request anchored at now) and waits for its answer. It
// returns how many new rows were stored and the phone's answer, if any.
func (rs *ReadState) fetchLatest(ctx context.Context, j string, count int, wait time.Duration) (int, *waHistorySync.Conversation, error) {
	chat, err := types.ParseJID(j)
	if err != nil {
		return 0, nil, err
	}
	u := unreadSyncer
	ch := make(chan *waHistorySync.Conversation, 1)
	u.mu.Lock()
	if u.waiters == nil {
		u.waiters = map[string]chan *waHistorySync.Conversation{}
	}
	u.waiters[j] = ch
	u.mu.Unlock()
	defer func() { u.mu.Lock(); delete(u.waiters, j); u.mu.Unlock() }()

	var before, after int
	rs.db.QueryRow(`SELECT COUNT(*) FROM messages WHERE chat_jid = ?`, j).Scan(&before)
	info := &types.MessageInfo{MessageSource: types.MessageSource{Chat: chat}, Timestamp: time.Now()}
	msg := rs.client.BuildHistorySyncRequest(info, count)
	if _, err := rs.client.SendMessage(ctx, rs.client.Store.ID.ToNonAD(), msg, whatsmeow.SendRequestExtra{Peer: true}); err != nil {
		return 0, nil, fmt.Errorf("history request failed: %v", err)
	}
	var conv *waHistorySync.Conversation
	select {
	case conv = <-ch:
	case <-time.After(wait):
	case <-ctx.Done():
	}
	rs.db.QueryRow(`SELECT COUNT(*) FROM messages WHERE chat_jid = ?`, j).Scan(&after)
	if conv == nil {
		return after - before, nil, fmt.Errorf("phone didn't answer within %s", wait)
	}
	return after - before, conv, nil
}

// AutoSyncUnread runs once after a (re)connect, at most every 10 minutes.
func (rs *ReadState) AutoSyncUnread() {
	u := unreadSyncer
	u.mu.Lock()
	recent := time.Since(u.lastRun) < 10*time.Minute
	u.mu.Unlock()
	if recent {
		return
	}
	time.Sleep(30 * time.Second) // let offline messages and app state settle
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	if res, err := rs.SyncUnread(ctx, 40); err != nil {
		rs.log.Warnf("Auto unread sync: %v", err)
	} else {
		rs.log.Infof("Auto unread sync: %d chats checked, %d fixed, %d messages backfilled",
			res["chats_checked"], res["chats_fixed"], res["messages_backfilled"])
	}
}

func (rs *ReadState) registerUnreadSyncHandlers() {
	http.HandleFunc("/api/sync/unread", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req struct {
			MaxChats int `json:"max_chats"`
		}
		json.NewDecoder(r.Body).Decode(&req)
		ctx, cancel := context.WithTimeout(r.Context(), 9*time.Minute)
		defer cancel()
		res, err := rs.SyncUnread(ctx, req.MaxChats)
		if res == nil {
			res = map[string]any{}
		}
		writeJSON(w, err, res)
	})
	http.HandleFunc("/api/unread-state", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, nil, rs.UnreadState())
	})
}
