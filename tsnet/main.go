// sharebox-tsnet is a small sidecar that joins a Tailscale tailnet with tsnet
// and exposes a local SOCKS5 proxy on 127.0.0.1. ShareBox uses that proxy to
// reach peers' 100.x addresses without installing the Tailscale client.
//
// The auth key is never compiled into the binary. It is read at runtime from
// SHAREBOX_TS_AUTHKEY or SHAREBOX_TS_AUTHKEY_FILE.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"tailscale.com/tsnet"
)

var version = "dev"

type config struct {
	authKey   string
	hostname  string
	stateDir  string
	listen    string
	status    string
	control   string
	verbose   bool
}

type statusPayload struct {
	Ready    bool   `json:"ready"`
	Version  string `json:"version"`
	Listen   string `json:"listen"`
	IP       string `json:"ip"`
	Hostname string `json:"hostname"`
}

func main() {
	log.SetFlags(log.LstdFlags)
	log.SetOutput(os.Stderr)

	showVersion := flag.Bool("version", false, "print version and exit")
	flag.Parse()
	if *showVersion {
		fmt.Println(version)
		return
	}

	cfg, err := loadConfig()
	if err != nil {
		log.Printf("config error: %v", err)
		os.Exit(2)
	}

	srv := &tsnet.Server{
		Hostname:  cfg.hostname,
		AuthKey:   cfg.authKey,
		Dir:       cfg.stateDir,
		ControlURL: cfg.control,
		RunWebClient: false,
		Logf: func(format string, args ...any) {
			if cfg.verbose {
				log.Printf(format, args...)
			}
		},
	}
	defer srv.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	ip, err := waitForIP(ctx, srv)
	if err != nil {
		log.Printf("tailnet join failed: %v", err)
		os.Exit(1)
	}

	ln, err := net.Listen("tcp", cfg.listen)
	if err != nil {
		log.Printf("listen %s failed: %v", cfg.listen, err)
		os.Exit(1)
	}
	defer ln.Close()

	payload := statusPayload{
		Ready: true, Version: version, Listen: cfg.listen, IP: ip, Hostname: cfg.hostname,
	}
	writeStatus(cfg.status, payload)
	if line, err := json.Marshal(payload); err == nil {
		fmt.Println(string(line))
	}
	log.Printf("ready: socks5=%s tailnet_ip=%s hostname=%s", cfg.listen, ip, cfg.hostname)

	go func() {
		<-ctx.Done()
		ln.Close()
	}()

	if err := serveSOCKS(ctx, ln, srv.Dial); err != nil && ctx.Err() == nil {
		log.Printf("socks server stopped: %v", err)
		os.Exit(1)
	}
}

func loadConfig() (config, error) {
	key := strings.TrimSpace(os.Getenv("SHAREBOX_TS_AUTHKEY"))
	if key == "" {
		if path := strings.TrimSpace(os.Getenv("SHAREBOX_TS_AUTHKEY_FILE")); path != "" {
			data, err := os.ReadFile(path)
			if err != nil {
				return config{}, fmt.Errorf("read auth key file: %w", err)
			}
			key = strings.TrimSpace(string(data))
		}
	}
	if key == "" {
		return config{}, errors.New("SHAREBOX_TS_AUTHKEY or SHAREBOX_TS_AUTHKEY_FILE is required")
	}

	hostname := strings.TrimSpace(os.Getenv("SHAREBOX_TS_HOSTNAME"))
	if hostname == "" {
		hostname = "sharebox-" + randomHex(3)
	}

	stateDir := strings.TrimSpace(os.Getenv("SHAREBOX_TS_STATE"))
	if stateDir == "" {
		base := os.Getenv("LOCALAPPDATA")
		if base == "" {
			if dir, err := os.UserConfigDir(); err == nil {
				base = dir
			} else {
				base = "."
			}
		}
		stateDir = filepath.Join(base, "ShareBox", "tsnet")
	}
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		return config{}, fmt.Errorf("create state dir: %w", err)
	}

	listen := strings.TrimSpace(os.Getenv("SHAREBOX_TS_LISTEN"))
	if listen == "" {
		listen = "127.0.0.1:1080"
	}

	return config{
		authKey:  key,
		hostname: hostname,
		stateDir: stateDir,
		listen:   listen,
		status:   strings.TrimSpace(os.Getenv("SHAREBOX_TS_STATUS_FILE")),
		control:  strings.TrimSpace(os.Getenv("SHAREBOX_TS_CONTROL_URL")),
		verbose:  os.Getenv("SHAREBOX_TS_VERBOSE") == "1",
	}, nil
}

func waitForIP(ctx context.Context, srv *tsnet.Server) (string, error) {
	lc, err := srv.LocalClient()
	if err != nil {
		return "", fmt.Errorf("local client: %w", err)
	}
	for {
		st, err := lc.Status(ctx)
		if err == nil && st != nil && st.Self != nil && len(st.Self.TailscaleIPs) > 0 {
			return st.Self.TailscaleIPs[0].String(), nil
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(500 * time.Millisecond):
		}
	}
}

func writeStatus(path string, payload statusPayload) {
	if path == "" {
		return
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return
	}
	_ = os.Rename(tmp, path)
}

func randomHex(n int) string {
	buf := make([]byte, n)
	if _, err := rand.Read(buf); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano()%1000000)
	}
	return hex.EncodeToString(buf)
}
