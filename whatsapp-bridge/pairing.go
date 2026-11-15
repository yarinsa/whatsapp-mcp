package main

import (
	"encoding/json"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
)

// Pairing state, shown by the connector's login page so you can scan the QR
// code there instead of digging it out of the deploy logs.
var pairing = struct {
	sync.Mutex
	code    string
	expires time.Time
}{}

func setPairingCode(code string, ttl time.Duration) {
	pairing.Lock()
	pairing.code, pairing.expires = code, time.Now().Add(ttl)
	pairing.Unlock()
}

// ownerPhone is the only WhatsApp account allowed to pair this bridge.
func ownerPhone() string {
	return strings.TrimPrefix(strings.TrimSpace(os.Getenv("WHATSAPP_OWNER_PHONE")), "+")
}

func registerPairingHandler(client *whatsmeow.Client) {
	http.HandleFunc("/api/pairing", func(w http.ResponseWriter, r *http.Request) {
		paired := client != nil && client.Store != nil && client.Store.ID != nil
		out := map[string]any{"paired": paired, "connected": paired && client.IsConnected()}
		if !paired {
			pairing.Lock()
			if pairing.code != "" && time.Now().Before(pairing.expires) {
				out["qr"] = pairing.code
			}
			pairing.Unlock()
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(out)
	})
}
