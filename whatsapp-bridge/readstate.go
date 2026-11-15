package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

// migrateReadState adds unread tracking to chats and an index for
// "latest message per chat" lookups. Additive only.
func migrateReadState(db *sql.DB) error {
	for _, col := range []string{
		`ALTER TABLE chats ADD COLUMN unread_count INTEGER NOT NULL DEFAULT 0`,
		`ALTER TABLE chats ADD COLUMN marked_unread BOOLEAN NOT NULL DEFAULT 0`,
	} {
		if _, err := db.Exec(col); err != nil && !strings.Contains(err.Error(), "duplicate column") {
			return err
		}
	}
	_, err := db.Exec(`CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_jid, timestamp)`)
	return err
}

// repairLastMessageTimes points every chat's last_message_time at its newest
// stored message. Older code bumped it for messages that were never stored.
func repairLastMessageTimes(db *sql.DB, log waLog.Logger) {
	res, err := db.Exec(`
		UPDATE chats SET last_message_time = (
			SELECT m.timestamp FROM messages m WHERE m.chat_jid = chats.jid
			ORDER BY julianday(m.timestamp) DESC LIMIT 1)
		WHERE EXISTS (SELECT 1 FROM messages m WHERE m.chat_jid = chats.jid)
		  AND COALESCE(last_message_time, '') != (
			SELECT m.timestamp FROM messages m WHERE m.chat_jid = chats.jid
			ORDER BY julianday(m.timestamp) DESC LIMIT 1)`)
	if err != nil {
		log.Warnf("Failed to repair last message times: %v", err)
		return
	}
	if n, _ := res.RowsAffected(); n > 0 {
		log.Infof("Repaired last_message_time for %d chats", n)
	}
}

// ReadState tracks unread counts and marks chats read or unread on WhatsApp.
type ReadState struct {
	client *whatsmeow.Client
	db     *sql.DB
	log    waLog.Logger
}

func (rs *ReadState) setRead(chat string) {
	rs.db.Exec(`UPDATE chats SET unread_count = 0, marked_unread = 0 WHERE jid = ?`, chat)
}

// OnStoredMessage updates unread counts for a message that was just stored.
func (rs *ReadState) OnStoredMessage(chat string, fromMe bool) {
	if fromMe {
		rs.setRead(chat) // replying means you've read the chat
		return
	}
	rs.db.Exec(`UPDATE chats SET unread_count = unread_count + 1 WHERE jid = ?`, chat)
}

// HandleEvent follows reads done on your other devices.
func (rs *ReadState) HandleEvent(evt any) {
	switch v := evt.(type) {
	case *events.Receipt:
		if v.IsFromMe && (v.Type == types.ReceiptTypeRead || v.Type == types.ReceiptTypeReadSelf) {
			rs.setRead(v.Chat.String())
		}
	case *events.MarkChatAsRead:
		if v.Action.GetRead() {
			rs.setRead(v.JID.String())
		} else {
			rs.db.Exec(`UPDATE chats SET marked_unread = 1 WHERE jid = ?`, v.JID.String())
		}
	}
}

// ApplyHistory sets unread state from a history-sync conversation.
func (rs *ReadState) ApplyHistory(chat string, unread uint32, markedUnread bool) {
	rs.db.Exec(`UPDATE chats SET unread_count = ?, marked_unread = ? WHERE jid = ?`, unread, markedUnread, chat)
}

type storedMsg struct {
	id, sender string
	ts         time.Time
	fromMe     bool
}

func (rs *ReadState) recentMessages(chat string, limit int) ([]storedMsg, error) {
	rows, err := rs.db.Query(`
		SELECT id, sender, timestamp, is_from_me FROM messages
		WHERE chat_jid = ? ORDER BY julianday(timestamp) DESC LIMIT ?`, chat, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []storedMsg
	for rows.Next() {
		var m storedMsg
		if rows.Scan(&m.id, &m.sender, &m.ts, &m.fromMe) == nil {
			out = append(out, m)
		}
	}
	return out, nil
}

// senderJID turns a stored sender ("972…", "2403…", or a full JID) into a JID.
func senderJID(l lookups, s string) types.JID {
	if j, ok := parseUser(l, s); ok {
		return j
	}
	return types.JID{}
}

// MarkRead sends read receipts for the chat's unread incoming messages and
// syncs the read state to your other devices. With read=false it marks the
// chat unread instead (no receipts are withdrawn: WhatsApp can't do that).
func (rs *ReadState) MarkRead(ctx context.Context, chatJID string, read bool) (string, error) {
	if !rs.client.IsConnected() {
		return "", fmt.Errorf("not connected to WhatsApp")
	}
	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return "", fmt.Errorf("invalid chat JID: %v", err)
	}
	var unread int
	if err := rs.db.QueryRow(`SELECT unread_count FROM chats WHERE jid = ?`, chatJID).Scan(&unread); err != nil {
		return "", fmt.Errorf("unknown chat %s", chatJID)
	}
	msgs, err := rs.recentMessages(chatJID, 100)
	if err != nil {
		return "", err
	}
	if len(msgs) == 0 {
		return "", fmt.Errorf("no stored messages in %s", chatJID)
	}

	// Message key of the newest message anchors the app-state range.
	last := msgs[0]
	key := &waCommon.MessageKey{RemoteJID: proto.String(chatJID), FromMe: proto.Bool(last.fromMe), ID: proto.String(last.id)}
	if chat.Server == types.GroupServer && !last.fromMe {
		if s := senderJID(names.storeLookups(ctx), last.sender); !s.IsEmpty() {
			key.Participant = proto.String(s.String())
		}
	}

	if !read {
		if err := rs.client.SendAppState(ctx, appstate.BuildMarkChatAsRead(chat, false, last.ts, key)); err != nil {
			return "", fmt.Errorf("mark unread: %w", err)
		}
		rs.db.Exec(`UPDATE chats SET marked_unread = 1 WHERE jid = ?`, chatJID)
		return "Marked as unread", nil
	}

	// Incoming messages since your last own message, grouped by sender for
	// group chats (receipts are per sender).
	l := names.storeLookups(ctx)
	bySender := map[types.JID][]types.MessageID{}
	count := 0
	for _, m := range msgs {
		if m.fromMe {
			break
		}
		sender := types.EmptyJID
		if chat.Server == types.GroupServer {
			if sender = senderJID(l, m.sender); sender.IsEmpty() {
				continue
			}
		}
		bySender[sender] = append(bySender[sender], m.id)
		count++
	}
	for sender, ids := range bySender {
		if err := rs.client.MarkRead(ctx, ids, time.Now(), chat, sender); err != nil {
			return "", fmt.Errorf("send read receipts: %w", err)
		}
	}
	if err := rs.client.SendAppState(ctx, appstate.BuildMarkChatAsRead(chat, true, last.ts, key)); err != nil {
		rs.log.Warnf("Read receipts sent but syncing read state failed for %s: %v", chatJID, err)
	}
	rs.setRead(chatJID)
	return fmt.Sprintf("Marked as read (%d messages)", count), nil
}

func (rs *ReadState) registerHandler() {
	http.HandleFunc("/api/mark-read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req struct {
			ChatJID string `json:"chat_jid"`
			Read    *bool  `json:"read"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" {
			http.Error(w, "chat_jid is required", http.StatusBadRequest)
			return
		}
		read := req.Read == nil || *req.Read
		msg, err := rs.MarkRead(r.Context(), req.ChatJID, read)
		w.Header().Set("Content-Type", "application/json")
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]any{"success": false, "message": err.Error()})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"success": true, "message": msg})
	})
}
