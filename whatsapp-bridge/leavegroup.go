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
	what := "group"
	info, infoErr := rs.client.GetGroupInfo(ctx, chat)
	if infoErr == nil && info.IsDefaultSubGroup && !info.LinkedParentJID.IsEmpty() {
		// A community's announcement group can't be left on its own: leave the community.
		chat = info.LinkedParentJID
		chatJID = chat.String()
		info, infoErr = rs.client.GetGroupInfo(ctx, chat)
	}
	if infoErr == nil && info.IsParent {
		what = "community"
		subs, err := rs.client.GetSubGroups(ctx, chat)
		if err != nil {
			return "", fmt.Errorf("list community groups: %w", err)
		}
		left := 0
		for _, sub := range subs {
			if sub.IsDefaultSubGroup {
				continue // goes with the community itself
			}
			if rs.client.LeaveGroup(ctx, sub.JID) == nil { // fails for groups you aren't in
				left++
			}
		}
		what = fmt.Sprintf("community and %d of its groups", left)
	}
	if err := rs.client.LeaveGroup(ctx, chat); err != nil {
		return "", fmt.Errorf("leave %s: %w", what, err)
	}
	if !deleteChat {
		return "Left the " + what + " (the chat stays in your list)", nil
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
	return "Left the " + what + " and deleted the chat on all devices", nil
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

// SyncChannelNames stores the real names of followed channels, which history
// sync leaves as bare numbers, so chat lists and approval prompts show them.
func (rs *ReadState) SyncChannelNames(ctx context.Context) {
	list, err := rs.client.GetSubscribedNewsletters(ctx)
	if err != nil {
		return
	}
	for _, n := range list {
		if name := n.ThreadMeta.Name.Text; name != "" {
			rs.db.Exec(`UPDATE chats SET name = ? WHERE jid = ?`, name, n.ID.String())
		}
	}
}

// registerChannelHandlers serves the followed channels (newsletters) and unfollowing them.
func (rs *ReadState) registerChannelHandlers() {
	http.HandleFunc("/api/channels", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		list, err := rs.client.GetSubscribedNewsletters(r.Context())
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]any{"success": false, "message": err.Error()})
			return
		}
		out := []map[string]any{}
		for _, n := range list {
			if n.ThreadMeta.Name.Text != "" {
				rs.db.Exec(`UPDATE chats SET name = ? WHERE jid = ?`, n.ThreadMeta.Name.Text, n.ID.String())
			}
			role := ""
			if n.ViewerMeta != nil {
				role = string(n.ViewerMeta.Role)
			}
			out = append(out, map[string]any{"jid": n.ID.String(), "name": n.ThreadMeta.Name.Text,
				"subscribers": n.ThreadMeta.SubscriberCount, "role": role})
		}
		json.NewEncoder(w).Encode(map[string]any{"success": true, "channels": out})
	})
	http.HandleFunc("/api/unfollow-channel", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		var req struct {
			JID string `json:"jid"`
		}
		json.NewDecoder(r.Body).Decode(&req)
		jid, err := types.ParseJID(req.JID)
		if err == nil && jid.Server != types.NewsletterServer {
			err = fmt.Errorf("not a channel JID: %s", req.JID)
		}
		if err == nil {
			err = rs.client.UnfollowNewsletter(r.Context(), jid)
		}
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]any{"success": false, "message": err.Error()})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"success": true, "message": "Unfollowed the channel"})
	})
}
