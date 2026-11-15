package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/types"
)

// LeaveGroup exits a group. With deleteChat it also deletes the chat (and
// its media) on all your devices and drops it from the local store.
func (rs *ReadState) LeaveGroup(ctx context.Context, chatJID string, deleteChat bool) (string, error) {
	if !rs.client.IsConnected() {
		return "", fmt.Errorf("not connected to WhatsApp")
	}
	chat, err := types.ParseJID(chatJID)
	if err != nil || chat.Server != types.GroupServer {
		return "", fmt.Errorf("not a group JID: %s", chatJID)
	}
	if err := rs.client.LeaveGroup(ctx, chat); err != nil {
		return "", fmt.Errorf("leave group: %w", err)
	}
	if !deleteChat {
		return "Left the group (the chat stays in your list)", nil
	}
	ts, key, _, err := rs.anchor(ctx, chat)
	if err != nil {
		return "", fmt.Errorf("left the group, but deleting the chat failed: %w", err)
	}
	if err := rs.client.SendAppState(ctx, appstate.BuildDeleteChat(chat, ts, key, true)); err != nil {
		return "", fmt.Errorf("left the group, but deleting the chat failed: %w", err)
	}
	rs.db.Exec(`DELETE FROM messages WHERE chat_jid = ?`, chatJID)
	rs.db.Exec(`DELETE FROM chats WHERE jid = ?`, chatJID)
	return "Left the group and deleted the chat on all devices", nil
}

func (rs *ReadState) registerLeaveHandler() {
	http.HandleFunc("/api/leave-group", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req struct {
			ChatJID    string `json:"chat_jid"`
			DeleteChat bool   `json:"delete_chat"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" {
			http.Error(w, "chat_jid is required", http.StatusBadRequest)
			return
		}
		msg, err := rs.LeaveGroup(r.Context(), req.ChatJID, req.DeleteChat)
		w.Header().Set("Content-Type", "application/json")
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]any{"success": false, "message": err.Error()})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"success": true, "message": msg})
	})
}
