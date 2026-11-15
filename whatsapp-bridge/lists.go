package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

// WhatsApp chat lists ("Lists" in the chat filter bar) are labels in the
// regular app-state collection. We mirror them locally from app-state events.

func (rs *ReadState) migrateLists() error {
	_, err := rs.db.Exec(`
		CREATE TABLE IF NOT EXISTS lists (
			id TEXT PRIMARY KEY, name TEXT, color INTEGER, type INTEGER,
			order_index INTEGER, is_active BOOLEAN, deleted BOOLEAN NOT NULL DEFAULT 0);
		CREATE TABLE IF NOT EXISTS list_chats (
			list_id TEXT, chat_jid TEXT, PRIMARY KEY (list_id, chat_jid));`)
	return err
}

func (rs *ReadState) saveList(id string, a *waSyncAction.LabelEditAction) {
	rs.db.Exec(`INSERT INTO lists (id, name, color, type, order_index, is_active, deleted)
		VALUES (?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(id) DO UPDATE SET name=excluded.name, color=excluded.color, type=excluded.type,
		order_index=excluded.order_index, is_active=excluded.is_active, deleted=excluded.deleted`,
		id, a.GetName(), a.GetColor(), int32(a.GetType()), a.GetOrderIndex(), a.GetIsActive(), a.GetDeleted())
	if a.GetDeleted() {
		rs.db.Exec(`DELETE FROM list_chats WHERE list_id = ?`, id)
	}
}

func (rs *ReadState) saveListChat(id, chat string, on bool) {
	if on {
		rs.db.Exec(`INSERT OR IGNORE INTO list_chats (list_id, chat_jid) VALUES (?, ?)`, id, chat)
	} else {
		rs.db.Exec(`DELETE FROM list_chats WHERE list_id = ? AND chat_jid = ?`, id, chat)
	}
}

// HandleListEvent mirrors list edits made on any device.
func (rs *ReadState) HandleListEvent(evt any) {
	switch v := evt.(type) {
	case *events.LabelEdit:
		rs.saveList(v.LabelID, v.Action)
	case *events.LabelAssociationChat:
		rs.saveListChat(v.LabelID, v.JID.String(), v.Action.GetLabeled())
	}
}

type listInfo struct {
	ID    string     `json:"id"`
	Name  string     `json:"name"`
	Type  string     `json:"type"`
	Chats []listChat `json:"chats"`
}

type listChat struct {
	JID  string `json:"jid"`
	Name string `json:"name"`
}

// Lists returns stored lists. With refresh it first re-reads app state from
// WhatsApp (also done automatically when nothing is stored yet).
func (rs *ReadState) Lists(ctx context.Context, refresh bool) ([]listInfo, error) {
	var n int
	rs.db.QueryRow(`SELECT COUNT(*) FROM lists`).Scan(&n)
	if refresh || n == 0 {
		if !rs.client.IsConnected() {
			return nil, fmt.Errorf("not connected to WhatsApp")
		}
		rs.client.EmitAppStateEventsOnFullSync = true // full sync must emit label events so we can mirror them
		if err := rs.client.FetchAppState(ctx, appstate.WAPatchRegular, true, false); err != nil {
			return nil, fmt.Errorf("sync lists: %w", err)
		}
	}
	rows, err := rs.db.Query(`SELECT id, name, type FROM lists WHERE deleted = 0 ORDER BY order_index, CAST(id AS INTEGER)`)
	if err != nil {
		return nil, err
	}
	var out []listInfo
	for rows.Next() {
		var l listInfo
		var t int32
		rows.Scan(&l.ID, &l.Name, &t)
		l.Type = waSyncAction.LabelEditAction_ListType(t).String()
		l.Chats = []listChat{}
		out = append(out, l)
	}
	rows.Close()
	for i := range out {
		crows, err := rs.db.Query(`SELECT lc.chat_jid, COALESCE(c.name, '') FROM list_chats lc
			LEFT JOIN chats c ON c.jid = lc.chat_jid WHERE lc.list_id = ?`, out[i].ID)
		if err != nil {
			return nil, err
		}
		for crows.Next() {
			var c listChat
			crows.Scan(&c.JID, &c.Name)
			out[i].Chats = append(out[i].Chats, c)
		}
		crows.Close()
	}
	return out, nil
}

func (rs *ReadState) sendListEdit(ctx context.Context, id string, a *waSyncAction.LabelEditAction) error {
	patch := appstate.PatchInfo{Type: appstate.WAPatchRegular, Mutations: []appstate.MutationInfo{{
		Index: []string{appstate.IndexLabelEdit, id}, Version: 3,
		Value: &waSyncAction.SyncActionValue{LabelEditAction: a},
	}}}
	if err := rs.sendAppStateRetry(ctx, patch); err != nil {
		return err
	}
	rs.saveList(id, a)
	return nil
}

func (rs *ReadState) loadList(id string) (*waSyncAction.LabelEditAction, error) {
	var name string
	var color, typ, order int32
	var active bool
	err := rs.db.QueryRow(`SELECT name, color, type, order_index, is_active FROM lists WHERE id = ? AND deleted = 0`, id).
		Scan(&name, &color, &typ, &order, &active)
	if err != nil {
		return nil, fmt.Errorf("unknown list %s (call list_chat_lists to refresh)", id)
	}
	t := waSyncAction.LabelEditAction_ListType(typ)
	return &waSyncAction.LabelEditAction{Name: &name, Color: &color, Type: &t, OrderIndex: &order, IsActive: &active, Deleted: proto.Bool(false)}, nil
}

// CreateList makes a new custom list and returns its id.
func (rs *ReadState) CreateList(ctx context.Context, name string) (string, error) {
	if _, err := rs.Lists(ctx, false); err != nil {
		return "", err
	}
	var maxID, maxOrder int
	rs.db.QueryRow(`SELECT COALESCE(MAX(CAST(id AS INTEGER)), 0), COALESCE(MAX(order_index), 0) FROM lists`).Scan(&maxID, &maxOrder)
	id := strconv.Itoa(maxID + 1)
	color := int32((maxID + 1) % 20)
	a := &waSyncAction.LabelEditAction{
		Name: &name, Color: &color, Deleted: proto.Bool(false), IsActive: proto.Bool(true),
		OrderIndex: proto.Int32(int32(maxOrder + 1)), Type: waSyncAction.LabelEditAction_CUSTOM.Enum(),
	}
	return id, rs.sendListEdit(ctx, id, a)
}

// EditList renames or deletes a list.
func (rs *ReadState) EditList(ctx context.Context, id, name string, del bool) error {
	a, err := rs.loadList(id)
	if err != nil {
		return err
	}
	if a.GetType() != waSyncAction.LabelEditAction_CUSTOM && a.GetType() != waSyncAction.LabelEditAction_NONE {
		return fmt.Errorf("list %s is a built-in %s list and can't be edited", id, a.GetType())
	}
	if name != "" {
		a.Name = &name
	}
	a.Deleted = proto.Bool(del)
	return rs.sendListEdit(ctx, id, a)
}

// SetListChats adds or removes chats from a list. Returns per-chat errors.
func (rs *ReadState) SetListChats(ctx context.Context, id string, chats []string, add bool) (int, []string, error) {
	if _, err := rs.loadList(id); err != nil {
		return 0, nil, err
	}
	var muts []appstate.MutationInfo
	var bad []string
	var jids []string
	for _, c := range chats {
		j, err := types.ParseJID(c)
		if err != nil {
			bad = append(bad, c+": invalid JID")
			continue
		}
		muts = append(muts, appstate.MutationInfo{
			Index: []string{appstate.IndexLabelAssociationChat, id, j.String()}, Version: 3,
			Value: &waSyncAction.SyncActionValue{LabelAssociationAction: &waSyncAction.LabelAssociationAction{Labeled: proto.Bool(add)}},
		})
		jids = append(jids, j.String())
	}
	if len(muts) == 0 {
		return 0, bad, nil
	}
	if err := rs.sendAppStateRetry(ctx, appstate.PatchInfo{Type: appstate.WAPatchRegular, Mutations: muts}); err != nil {
		return 0, bad, err
	}
	for _, j := range jids {
		rs.saveListChat(id, j, add)
	}
	return len(jids), bad, nil
}

func writeJSON(w http.ResponseWriter, err error, body map[string]any) {
	w.Header().Set("Content-Type", "application/json")
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		json.NewEncoder(w).Encode(map[string]any{"success": false, "message": err.Error()})
		return
	}
	body["success"] = true
	json.NewEncoder(w).Encode(body)
}

func (rs *ReadState) registerListHandlers() {
	if err := rs.migrateLists(); err != nil {
		rs.log.Errorf("Failed to create list tables: %v", err)
	}
	http.HandleFunc("/api/lists", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
		defer cancel()
		lists, err := rs.Lists(ctx, r.URL.Query().Get("refresh") == "1")
		writeJSON(w, err, map[string]any{"lists": lists})
	})
	http.HandleFunc("/api/lists/create", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Name string `json:"name"`
		}
		if json.NewDecoder(r.Body).Decode(&req) != nil || req.Name == "" {
			http.Error(w, "name is required", http.StatusBadRequest)
			return
		}
		id, err := rs.CreateList(r.Context(), req.Name)
		writeJSON(w, err, map[string]any{"list_id": id, "message": "Created list " + req.Name})
	})
	http.HandleFunc("/api/lists/edit", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			ListID string `json:"list_id"`
			Name   string `json:"name"`
			Delete bool   `json:"delete"`
		}
		if json.NewDecoder(r.Body).Decode(&req) != nil || req.ListID == "" {
			http.Error(w, "list_id is required", http.StatusBadRequest)
			return
		}
		err := rs.EditList(r.Context(), req.ListID, req.Name, req.Delete)
		writeJSON(w, err, map[string]any{"message": "List updated"})
	})
	http.HandleFunc("/api/lists/chats", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			ListID   string   `json:"list_id"`
			ChatJIDs []string `json:"chat_jids"`
			Add      bool     `json:"add"`
		}
		if json.NewDecoder(r.Body).Decode(&req) != nil || req.ListID == "" || len(req.ChatJIDs) == 0 {
			http.Error(w, "list_id and chat_jids are required", http.StatusBadRequest)
			return
		}
		n, bad, err := rs.SetListChats(r.Context(), req.ListID, req.ChatJIDs, req.Add)
		writeJSON(w, err, map[string]any{"updated": n, "errors": bad})
	})
}

// sendAppStateRetry sends an app-state patch; if our copy of that collection
// has diverged from the server's, it downloads it from scratch and retries once.
func (rs *ReadState) sendAppStateRetry(ctx context.Context, patch appstate.PatchInfo) error {
	err := rs.client.SendAppState(ctx, patch)
	if err == nil || !appStateOutOfSync(err) {
		return err
	}
	rs.log.Warnf("App state %s out of sync (%v); resyncing", patch.Type, err)
	if ferr := rs.client.FetchAppState(ctx, patch.Type, true, false); ferr != nil {
		return fmt.Errorf("%w (resync also failed: %v)", err, ferr)
	}
	return rs.client.SendAppState(ctx, patch)
}
