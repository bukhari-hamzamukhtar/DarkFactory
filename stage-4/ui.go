package main

import (
	"bytes"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"html/template"
	"net/http"
	"path"
	"strings"
	"time"
)

// The browser product. Every byte of it -- markup, stylesheet, script -- is compiled
// into the binary, because §2 says runtime assets must be in the image and there is
// no outbound network to fetch a font or a framework from.
//
//go:embed web
var webAssets embed.FS

var pageTemplates = template.Must(template.ParseFS(webAssets, "web/*.html"))

// assetVersion stamps the stylesheet and script URLs so a new build is never served
// from a stale cache, without ever serving a stale asset for a fixed name.
var assetVersion = assetFingerprint()

func assetFingerprint() string {
	digest := sha256.New()
	for _, name := range []string{"web/app.css", "web/app.js", "web/app.html"} {
		content, err := webAssets.ReadFile(name)
		if err != nil {
			return "dev"
		}
		digest.Write(content)
	}
	return hex.EncodeToString(digest.Sum(nil))[:12]
}

// pageData is what a screen needs before the client script takes over. The restaurant
// list and today's date are rendered server-side so the search form is usable the
// moment the HTML lands, rather than after a round trip.
type pageData struct {
	Route       string
	Title       string
	Version     string
	Restaurants []*restaurant
	Today       string
}

var pageTitles = map[string]string{
	"search": "Find a table",
	"signup": "Create an account",
	"login":  "Sign in",
	"lookup": "My booking",
}

func (s *server) handlePage(w http.ResponseWriter, r *http.Request, route string) {
	data := pageData{
		Route:   route,
		Title:   pageTitles[route],
		Version: assetVersion,
		Today:   time.Now().UTC().Format(localDateLayout),
	}
	if route == "search" {
		s.mu.Lock()
		data.Restaurants = append([]*restaurant(nil), s.st.Restaurants...)
		if len(data.Restaurants) > 0 {
			// Today where the first restaurant is, which is the date a diner
			// standing in front of it would expect to see.
			data.Today = time.Now().In(data.Restaurants[0].loc).Format(localDateLayout)
		}
		s.mu.Unlock()
	}

	var rendered bytes.Buffer
	if err := pageTemplates.ExecuteTemplate(&rendered, "app.html", data); err != nil {
		writeError(w, errorf(http.StatusInternalServerError, "internal_error",
			"the page could not be rendered"))
		return
	}
	// A screen route answers HTML; §3.4's JSON convention governs the API (stage 2).
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(rendered.Bytes())
}

var assetTypes = map[string]string{
	".css": "text/css; charset=utf-8",
	".js":  "text/javascript; charset=utf-8",
	".svg": "image/svg+xml",
}

func (s *server) handleAsset(w http.ResponseWriter, r *http.Request, name string) {
	// Only the files that were embedded, by exact name: no traversal, no listing.
	if name == "" || strings.Contains(name, "..") || strings.Contains(name, "/") {
		writeError(w, notFound())
		return
	}
	contentType, known := assetTypes[strings.ToLower(path.Ext(name))]
	if !known {
		writeError(w, notFound())
		return
	}
	content, err := webAssets.ReadFile("web/" + name)
	if err != nil {
		writeError(w, notFound())
		return
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Cache-Control", "public, max-age=300")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(content)
}
