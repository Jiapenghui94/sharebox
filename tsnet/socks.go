package main

import (
	"bufio"
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"strconv"
	"time"
)

type dialFunc func(context.Context, string, string) (net.Conn, error)

func serveSOCKS(ctx context.Context, ln net.Listener, dial dialFunc) error {
	for {
		conn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		go handleSOCKS(ctx, conn, dial)
	}
}

func handleSOCKS(ctx context.Context, conn net.Conn, dial dialFunc) {
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(30 * time.Second))

	reader := bufio.NewReader(conn)
	header := make([]byte, 2)
	if _, err := io.ReadFull(reader, header); err != nil || header[0] != 5 {
		return
	}
	methods := make([]byte, int(header[1]))
	if _, err := io.ReadFull(reader, methods); err != nil {
		return
	}
	if _, err := conn.Write([]byte{5, 0}); err != nil {
		return
	}

	request := make([]byte, 4)
	if _, err := io.ReadFull(reader, request); err != nil || request[0] != 5 {
		return
	}
	if request[1] != 1 {
		writeReply(conn, 7) // command not supported
		return
	}
	host, err := readAddress(reader, request[3])
	if err != nil {
		writeReply(conn, 8) // address type not supported
		return
	}
	portBytes := make([]byte, 2)
	if _, err := io.ReadFull(reader, portBytes); err != nil {
		return
	}
	port := binary.BigEndian.Uint16(portBytes)
	target := net.JoinHostPort(host, strconv.Itoa(int(port)))

	dialCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	remote, err := dial(dialCtx, "tcp", target)
	if err != nil {
		writeReply(conn, 5) // connection refused
		return
	}
	defer remote.Close()

	if err := writeReply(conn, 0); err != nil {
		return
	}
	_ = conn.SetDeadline(time.Time{})
	_ = remote.SetDeadline(time.Time{})
	relay(conn, remote)
}

func readAddress(r io.Reader, atyp byte) (string, error) {
	switch atyp {
	case 1:
		buf := make([]byte, 4)
		if _, err := io.ReadFull(r, buf); err != nil {
			return "", err
		}
		return net.IP(buf).String(), nil
	case 3:
		length := make([]byte, 1)
		if _, err := io.ReadFull(r, length); err != nil {
			return "", err
		}
		buf := make([]byte, int(length[0]))
		if _, err := io.ReadFull(r, buf); err != nil {
			return "", err
		}
		return string(buf), nil
	case 4:
		buf := make([]byte, 16)
		if _, err := io.ReadFull(r, buf); err != nil {
			return "", err
		}
		return net.IP(buf).String(), nil
	default:
		return "", fmt.Errorf("unsupported address type %d", atyp)
	}
}

func writeReply(w io.Writer, code byte) error {
	_, err := w.Write([]byte{5, code, 0, 1, 0, 0, 0, 0, 0, 0})
	return err
}

func relay(a, b net.Conn) {
	done := make(chan struct{}, 2)
	copyOne := func(dst, src net.Conn) {
		_, _ = io.Copy(dst, src)
		if closer, ok := dst.(interface{ CloseWrite() error }); ok {
			_ = closer.CloseWrite()
		}
		done <- struct{}{}
	}
	go copyOne(a, b)
	go copyOne(b, a)
	<-done
	<-done
}
