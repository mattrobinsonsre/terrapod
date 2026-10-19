package main

import (
	"net"
	"testing"
	"time"
)

const dialTimeout = 500 * time.Millisecond

// outwardAddress returns a non-loopback IPv4 address of this host, or "" when
// there is none — which is the case in some sandboxes, where the isolation test
// skips rather than passing vacuously.
func outwardAddress(t *testing.T) string {
	t.Helper()
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return ""
	}
	for _, a := range addrs {
		if ipnet, ok := a.(*net.IPNet); ok && !ipnet.IP.IsLoopback() && ipnet.IP.To4() != nil {
			return ipnet.IP.String()
		}
	}
	return ""
}
