package main

import (
	"log"
	"net"
	"net/http"
	"os"
	"time"
)

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080" // §3.1's default
	}
	address := net.JoinHostPort("0.0.0.0", port)

	srv := &http.Server{
		Addr:    address,
		Handler: newServer(),
		// Generous against the per-request budget of §2, including the 10 seconds
		// the test control endpoints are allowed, so the service never cuts off a
		// request the requirements still consider in flight.
		ReadHeaderTimeout: 15 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	listener, err := net.Listen("tcp", address)
	if err != nil {
		log.Fatalf("cannot listen on %s: %v", address, err)
	}
	// Listening is the readiness signal: the store is in memory and already serving,
	// so the first /health call after this line answers 200 (§3.2).
	log.Printf("tablekeeper listening on %s", address)
	if err := srv.Serve(listener); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server stopped: %v", err)
	}
}
