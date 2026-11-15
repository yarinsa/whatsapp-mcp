package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
)

const (
	serviceName = "whatsapp-bridge"
	// Bumped when the /api/health contract changes; the MCP server checks it.
	apiVersion = 1

	// After this long without a connection the process exits so launchd restarts it.
	reconnectGiveUp = 10 * time.Minute
	// After this long disconnected, show one macOS notification per outage.
	notifyAfter = 30 * time.Minute
)

// BridgeHealth tracks connection state and sync activity for /api/health.
type BridgeHealth struct {
	mu             sync.RWMutex
	startedAt      time.Time
	connected      bool
	loggedOut      bool
	connectedAt    time.Time
	disconnectedAt time.Time // start of the current outage; zero while connected
	lastMessageAt  time.Time // newest message timestamp seen (live or history)
	lastEventAt    time.Time // last message or history-sync event received
	notified       bool
}

var health = &BridgeHealth{startedAt: time.Now(), disconnectedAt: time.Now()}

func (h *BridgeHealth) SetConnected() {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.connected, h.loggedOut, h.notified = true, false, false
	h.connectedAt = time.Now()
	h.disconnectedAt = time.Time{}
}

func (h *BridgeHealth) SetDisconnected(loggedOut bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.connected || h.disconnectedAt.IsZero() {
		h.disconnectedAt = time.Now()
	}
	h.connected = false
	h.loggedOut = h.loggedOut || loggedOut
}

// Touch records a sync event, and a message timestamp when msgTime is non-zero.
func (h *BridgeHealth) Touch(msgTime time.Time) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.lastEventAt = time.Now()
	if msgTime.After(h.lastMessageAt) {
		h.lastMessageAt = msgTime
	}
}

func (h *BridgeHealth) SeedLastMessage(t time.Time) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if t.After(h.lastMessageAt) {
		h.lastMessageAt = t
	}
}

type healthResponse struct {
	Service                 string     `json:"service"`
	APIVersion              int        `json:"api_version"`
	PID                     int        `json:"pid"`
	Status                  string     `json:"status"` // ok | disconnected | logged_out
	Connected               bool       `json:"connected"`
	WebsocketConnected      bool       `json:"websocket_connected"`
	LoggedIn                bool       `json:"logged_in"`
	StartedAt               time.Time  `json:"started_at"`
	UptimeSeconds           int64      `json:"uptime_seconds"`
	ConnectedSince          *time.Time `json:"connected_since"`
	DisconnectedSince       *time.Time `json:"disconnected_since"`
	LastMessageAt           *time.Time `json:"last_message_at"`
	LastEventAt             *time.Time `json:"last_event_at"`
	SecondsSinceLastEvent   *int64     `json:"seconds_since_last_event"`
	SecondsSinceLastMessage *int64     `json:"seconds_since_last_message"`
}

func timePtr(t time.Time) *time.Time {
	if t.IsZero() {
		return nil
	}
	return &t
}

func since(t time.Time) *int64 {
	if t.IsZero() {
		return nil
	}
	s := int64(time.Since(t).Seconds())
	return &s
}

func (h *BridgeHealth) Snapshot(client *whatsmeow.Client) healthResponse {
	h.mu.RLock()
	defer h.mu.RUnlock()
	status := "ok"
	if h.loggedOut {
		status = "logged_out"
	} else if !h.connected {
		status = "disconnected"
	}
	return healthResponse{
		Service:                 serviceName,
		APIVersion:              apiVersion,
		PID:                     os.Getpid(),
		Status:                  status,
		Connected:               h.connected,
		WebsocketConnected:      client.IsConnected(),
		LoggedIn:                client.IsLoggedIn(),
		StartedAt:               h.startedAt,
		UptimeSeconds:           int64(time.Since(h.startedAt).Seconds()),
		ConnectedSince:          timePtr(h.connectedAt),
		DisconnectedSince:       timePtr(h.disconnectedAt),
		LastMessageAt:           timePtr(h.lastMessageAt),
		LastEventAt:             timePtr(h.lastEventAt),
		SecondsSinceLastEvent:   since(h.lastEventAt),
		SecondsSinceLastMessage: since(h.lastMessageAt),
	}
}

func registerHealthHandler(client *whatsmeow.Client) {
	http.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-WhatsApp-Bridge", "1")
		json.NewEncoder(w).Encode(health.Snapshot(client))
	})
}

// runWatchdog notifies once per long outage and exits the process when the
// client hasn't reconnected for reconnectGiveUp, so launchd starts a fresh one.
// A logged-out session is not restarted: it needs a QR scan, not a restart.
func runWatchdog() {
	for range time.Tick(30 * time.Second) {
		health.mu.Lock()
		down := health.disconnectedAt
		loggedOut, notified := health.loggedOut, health.notified
		if !down.IsZero() && time.Since(down) > notifyAfter && !notified {
			health.notified = true
		}
		health.mu.Unlock()

		if down.IsZero() {
			continue
		}
		outage := time.Since(down)
		if outage > notifyAfter && !notified {
			msg := fmt.Sprintf("Disconnected for %d min", int(outage.Minutes()))
			if loggedOut {
				msg = "Logged out. Run the bridge in a terminal and scan the QR code."
			}
			notify("WhatsApp bridge", msg)
		}
		if outage > reconnectGiveUp && !loggedOut {
			fmt.Printf("No WhatsApp connection for %s, exiting so launchd restarts the bridge\n", outage.Round(time.Second))
			os.Exit(2)
		}
	}
}

// notify shows a macOS notification; it is a no-op elsewhere.
func notify(title, msg string) {
	script := fmt.Sprintf("display notification %q with title %q", msg, title)
	if err := exec.Command("osascript", "-e", script).Run(); err != nil {
		fmt.Printf("notification failed: %v\n", err)
	}
}
