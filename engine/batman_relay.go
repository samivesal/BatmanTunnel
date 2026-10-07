package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"
)

type batmanListener struct {
	Bind     string `json:"bind"`
	Key      string `json:"key"`
	Protocol string `json:"protocol"`
}
type batmanRelayConfig struct {
	Listeners  []batmanListener  `json:"listeners"`
	Targets    map[string]string `json:"targets"`
	Generation string            `json:"generation"`
}
type batmanRouter struct {
	mu    sync.RWMutex
	cfg   batmanRelayConfig
	path  string
	slots chan struct{}
}

func (r *batmanRouter) target(key string) (string, string) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.cfg.Targets[key], r.cfg.Generation
}
func (r *batmanRouter) load() error {
	b, err := os.ReadFile(r.path)
	if err != nil {
		return err
	}
	var c batmanRelayConfig
	if err = json.Unmarshal(b, &c); err != nil {
		return err
	}
	r.mu.Lock()
	r.cfg = c
	r.mu.Unlock()
	return nil
}
func (r *batmanRouter) tcp(ctx context.Context, spec batmanListener) {
	for ctx.Err() == nil {
		l, err := net.Listen("tcp", spec.Bind)
		if err != nil {
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Second):
				continue
			}
		}
		go func() { <-ctx.Done(); l.Close() }()
		for ctx.Err() == nil {
			a, err := l.Accept()
			if err != nil {
				break
			}
			select {
			case r.slots <- struct{}{}:
			default:
				a.Close()
				continue
			}
			go func(a net.Conn) {
				defer func() { <-r.slots }()
				defer a.Close()
				target, _ := r.target(spec.Key)
				if target == "" {
					return
				}
				b, err := net.DialTimeout("tcp", target, 4*time.Second)
				if err != nil {
					return
				}
				defer b.Close()
				done := make(chan struct{}, 2)
				copyHalf := func(dst, src net.Conn) {
					_, _ = io.Copy(dst, src)
					if t, ok := dst.(*net.TCPConn); ok {
						_ = t.CloseWrite()
					}
					done <- struct{}{}
				}
				go copyHalf(a, b)
				go copyHalf(b, a)
				select {
				case <-ctx.Done():
					return
				case <-done:
				}
				select {
				case <-ctx.Done():
					return
				case <-done:
				}
			}(a)
		}
		l.Close()
	}
}

type batmanUDPFlow struct {
	c          *net.UDPConn
	generation string
	touched    time.Time
}

func (r *batmanRouter) udp(ctx context.Context, spec batmanListener) {
	for ctx.Err() == nil {
		addr, err := net.ResolveUDPAddr("udp", spec.Bind)
		if err != nil {
			return
		}
		l, err := net.ListenUDP("udp", addr)
		if err != nil {
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Second):
				continue
			}
		}
		var mu sync.Mutex
		flows := map[string]*batmanUDPFlow{}
		go func() {
			<-ctx.Done()
			l.Close()
			mu.Lock()
			defer mu.Unlock()
			for _, f := range flows {
				f.c.Close()
			}
		}()
		go func() {
			tick := time.NewTicker(10 * time.Second)
			defer tick.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-tick.C:
					mu.Lock()
					for key, f := range flows {
						if time.Since(f.touched) > 60*time.Second {
							f.c.Close()
							delete(flows, key)
						}
					}
					mu.Unlock()
				}
			}
		}()
		buf := make([]byte, 65535)
		for ctx.Err() == nil {
			n, from, err := l.ReadFromUDP(buf)
			if err != nil {
				break
			}
			target, generation := r.target(spec.Key)
			if target == "" {
				continue
			}
			key := from.String()
			mu.Lock()
			f := flows[key]
			if f != nil && f.generation != generation {
				f.c.Close()
				delete(flows, key)
				f = nil
			}
			if f == nil {
				if len(flows) >= 4096 {
					mu.Unlock()
					continue
				}
				to, e := net.ResolveUDPAddr("udp", target)
				if e != nil {
					mu.Unlock()
					continue
				}
				c, e := net.DialUDP("udp", nil, to)
				if e != nil {
					mu.Unlock()
					continue
				}
				f = &batmanUDPFlow{c: c, generation: generation, touched: time.Now()}
				flows[key] = f
				go func(f *batmanUDPFlow, from *net.UDPAddr, key string) {
					defer f.c.Close()
					b := make([]byte, 65535)
					for {
						_ = f.c.SetReadDeadline(time.Now().Add(65 * time.Second))
						n, e := f.c.Read(b)
						if e != nil {
							mu.Lock()
							if flows[key] == f {
								delete(flows, key)
							}
							mu.Unlock()
							return
						}
						_, _ = l.WriteToUDP(b[:n], from)
					}
				}(f, from, key)
			}
			f.touched = time.Now()
			c := f.c
			mu.Unlock()
			_, _ = c.Write(buf[:n])
		}
		l.Close()
	}
}
func runBatmanRelay(path string) {
	r := &batmanRouter{path: path, slots: make(chan struct{}, 32768)}
	if err := r.load(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	r.mu.RLock()
	specs := append([]batmanListener(nil), r.cfg.Listeners...)
	r.mu.RUnlock()
	for _, s := range specs {
		if s.Protocol == "tcp" || s.Protocol == "both" {
			go r.tcp(ctx, s)
		}
		if s.Protocol == "udp" || s.Protocol == "both" {
			go r.udp(ctx, s)
		}
	}
	ticker := time.NewTicker(200 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			_ = r.load()
		}
	}
}
