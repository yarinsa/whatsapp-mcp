package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
)

// FetchHistory asks the phone for up to count messages older than the oldest
// one stored for chat. The phone answers with an ON_DEMAND history sync, which
// handleHistorySync stores like any other. It waits up to wait for the answer
// and returns how many new messages arrived.
func (rs *ReadState) FetchHistory(ctx context.Context, chatJID string, count int, wait time.Duration) (int, string, error) {
	if rs.client == nil || !rs.client.IsConnected() || rs.client.Store.ID == nil {
		return 0, "", fmt.Errorf("not connected to WhatsApp")
	}
	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return 0, "", fmt.Errorf("invalid chat JID: %v", err)
	}
	if count <= 0 || count > 500 {
		count = 50
	}

	info := &types.MessageInfo{MessageSource: types.MessageSource{Chat: chat}}
	var id sql.NullString
	var ts sql.NullTime
	var fromMe sql.NullBool
	rs.db.QueryRow(`SELECT id, timestamp, is_from_me FROM messages WHERE chat_jid = ?
		ORDER BY julianday(timestamp) ASC LIMIT 1`, chatJID).Scan(&id, &ts, &fromMe)
	anchored := id.Valid && ts.Valid
	if anchored {
		info.ID, info.Timestamp, info.IsFromMe = id.String, ts.Time, fromMe.Bool
	} else {
		// No stored message to anchor on: ask for history before now.
		info.Timestamp = time.Now()
	}

	var before int
	rs.db.QueryRow(`SELECT COUNT(*) FROM messages WHERE chat_jid = ?`, chatJID).Scan(&before)

	msg := rs.client.BuildHistorySyncRequest(info, count)
	if _, err := rs.client.SendMessage(ctx, rs.client.Store.ID.ToNonAD(), msg, whatsmeow.SendRequestExtra{Peer: true}); err != nil {
		return 0, "", fmt.Errorf("history request failed: %v", err)
	}

	deadline := time.Now().Add(wait)
	after := before
	for time.Now().Before(deadline) {
		time.Sleep(time.Second)
		rs.db.QueryRow(`SELECT COUNT(*) FROM messages WHERE chat_jid = ?`, chatJID).Scan(&after)
		if after > before {
			time.Sleep(2 * time.Second) // let the rest of the batch land
			rs.db.QueryRow(`SELECT COUNT(*) FROM messages WHERE chat_jid = ?`, chatJID).Scan(&after)
			break
		}
	}
	note := ""
	if !anchored {
		note = "no stored messages to anchor on, so the phone may ignore the request"
	}
	return after - before, note, nil
}

func (rs *ReadState) registerFetchHistoryHandler() {
	http.HandleFunc("/api/fetch-history", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req struct {
			ChatJID     string `json:"chat_jid"`
			Count       int    `json:"count"`
			WaitSeconds int    `json:"wait_seconds"`
		}
		w.Header().Set("Content-Type", "application/json")
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]any{"success": false, "message": "chat_jid is required"})
			return
		}
		if req.WaitSeconds <= 0 || req.WaitSeconds > 60 {
			req.WaitSeconds = 20
		}
		n, note, err := rs.FetchHistory(r.Context(), req.ChatJID, req.Count, time.Duration(req.WaitSeconds)*time.Second)
		if err != nil {
			json.NewEncoder(w).Encode(map[string]any{"success": false, "message": err.Error()})
			return
		}
		msg := fmt.Sprintf("Fetched %d older messages", n)
		if n == 0 {
			msg = "No new messages arrived in time. The phone must be online; it may also have nothing older."
		}
		json.NewEncoder(w).Encode(map[string]any{"success": true, "fetched": n, "message": msg, "note": note})
	})
}
