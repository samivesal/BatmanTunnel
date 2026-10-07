package main

// BatmanTunnel additions. The native BackPack transports are retained unchanged.
import (
	"encoding/json"
	"fmt"
	"github.com/backpack/backpack/config"
	"github.com/backpack/backpack/internal/manage/tunnelspec"
	"os"
)

type batmanRenderRequest struct {
	Kind        string   `json:"kind"`
	Transport   string   `json:"transport"`
	Role        string   `json:"role"`
	Name        string   `json:"name"`
	Addr        string   `json:"addr"`
	Token       string   `json:"token"`
	Ports       []string `json:"ports"`
	LocalIP     string   `json:"local_ip"`
	PeerIP      string   `json:"peer_ip"`
	Interface   string   `json:"interface"`
	SNI         string   `json:"sni"`
	SpoofSource string   `json:"spoof_source"`
	SpoofPeer   string   `json:"spoof_peer"`
	AcceptUDP   bool     `json:"accept_udp"`
}

func batmanRender(r batmanRenderRequest) (string, error) {
	if r.Role != "iran" && r.Role != "abroad" {
		return "", fmt.Errorf("invalid role")
	}
	if len(r.Token) < 32 {
		return "", fmt.Errorf("token is too short")
	}
	if r.Kind == "reverse" {
		allowed := map[string]bool{"tcp": true, "tcpmux": true, "stealth": true, "pck": true, "udp": true, "kcp": true, "quic": true, "ws": true, "wsmux": true, "wss": true, "wssmux": true, "xdi": true}
		if !allowed[r.Transport] {
			return "", fmt.Errorf("unsupported reverse transport")
		}
		s := tunnelspec.Spec{Name: r.Name, Role: "client", RemoteAddr: r.Addr, Transport: r.Transport, Token: r.Token, AcceptUDP: r.AcceptUDP}
		if r.Role == "iran" {
			s.Role = "server"
			s.BindAddr = r.Addr
			s.Ports = r.Ports
		}
		tunnelspec.ApplyPreset(&s, "balance")
		s.LogLevel = "warn"
		s.ConnectionPool = 4
		s.KCPDataShards = 10
		s.KCPParityShards = 3
		return s.Render(), nil
	}
	if r.Kind != "direct" {
		return "", fmt.Errorf("unsupported tunnel kind")
	}
	allowed := map[string]bool{"udp": true, "quic": true, "pck": true, "xdi": true, "sni": true, "spoof": true}
	if !allowed[r.Transport] {
		return "", fmt.Errorf("unsupported direct carrier")
	}
	side := tunnelspec.SideIran
	if r.Role == "abroad" {
		side = tunnelspec.SideKharej
	}
	s := tunnelspec.L3Spec{Name: r.Name, Side: side, Carrier: r.Transport, Encap: "gre", Addr: r.Addr, Token: r.Token, Iface: r.Interface, LocalIP: r.LocalIP, PeerIP: r.PeerIP, MTU: 1280, SockBuf: 4194304, Ports: r.Ports, AcceptUDP: r.AcceptUDP, SNIDomain: r.SNI}
	if r.Transport == "spoof" {
		s.Spoof = config.SpoofConfig{SpoofProfile: "udp", SpoofSrcIP: r.SpoofSource, SpoofPeerIP: r.SpoofPeer}
	}
	return s.Render(), nil
}
func runBatmanRender() {
	var r batmanRenderRequest
	if err := json.NewDecoder(os.Stdin).Decode(&r); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	body, err := batmanRender(r)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	fmt.Print(body)
}
