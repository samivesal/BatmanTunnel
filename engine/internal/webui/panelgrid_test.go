package webui

import (
	"io/fs"
	"regexp"
	"strings"
	"testing"
)

// Everything on the metrics screen sits on one gutter.
//
// The section headings, the chart and the tables all keep 18px from the edge of
// the dialog. The three headline figures — Carrying now, Peak in the day, Up
// last 24 hours — sat directly in the body with none, so the one part of the
// screen that is pure reading ran edge to edge while everything around it was
// inset, and the numbers looked pressed into the corners.
func TestTheMetricsScreenKeepsOneGutter(t *testing.T) {
	loadPanel()

	css, err := fs.ReadFile(panelRoot, "css/screens/metrics.css")
	if err != nil {
		t.Fatalf("metrics.css: %v", err)
	}
	src := string(css)

	// The gutter every other block keeps, taken from the section heading rather
	// than written here — if that changes, this asks for the new one.
	sec := regexp.MustCompile(`\.dlg \.sec2\{[^}]*padding:\s*[\d.]+px\s+([\d.]+px)`).FindStringSubmatch(src)
	if sec == nil {
		t.Fatal("the section heading no longer declares a padding to line up with")
	}
	gutter := sec[1]

	rows := regexp.MustCompile(`\.dlg \.mrows\{([^}]*)\}`).FindStringSubmatch(src)
	if rows == nil {
		t.Fatal("the headline figures have no rule of their own")
	}
	if !strings.Contains(rows[1], "padding:0 "+gutter) {
		t.Errorf("the headline figures do not keep the screen's %s gutter, so they run "+
			"to the edge while every heading and chart around them is inset:\n  .mrows{%s}",
			gutter, rows[1])
	}

	// And the chart above them keeps it too, or the rows would line up with
	// nothing.
	if !regexp.MustCompile(`\.dlg \.chartbox\{[^}]*\s` + regexp.QuoteMeta(gutter)).MatchString(src) {
		t.Errorf("the chart does not keep the %s gutter", gutter)
	}
}

// Editing a server happens inside that server's own card, once.
//
// It used to insert a separate form after the card — a differently shaped box
// that broke the row and took the server's context away from the thing being
// edited — and it inserted another one on every press, so a server could end up
// with several open forms disagreeing about its address.
func TestEditingAServerHappensInsideItsCard(t *testing.T) {
	loadPanel()

	js, err := fs.ReadFile(panelRoot, "js/views/servers.js")
	if err != nil {
		t.Fatalf("servers.js: %v", err)
	}
	src := string(js)

	if strings.Contains(src, "openCredentials") {
		t.Error("the separate credentials form is still there")
	}
	if !strings.Contains(src, "card.append(editor)") {
		t.Error("the editor is not part of the card, so it cannot be the one instance " +
			"a server has")
	}
	if !strings.Contains(src, "classList.toggle('ed7')") {
		t.Error("Edit does not toggle, so pressing it twice does not close what it opened")
	}
	if strings.Contains(src, ".after(box)") {
		t.Error("something is still inserted beside the card rather than into it")
	}

	// Its surface is the one the remove confirmation already uses, which is what
	// makes the two read as one behaviour.
	css, err := fs.ReadFile(panelRoot, "css/components/servers.css")
	if err != nil {
		t.Fatalf("servers.css: %v", err)
	}
	for _, want := range []string{".mp7 .ed-l", ".mp7.ed7 .ed-l"} {
		if !strings.Contains(string(css), want) {
			t.Errorf("%s is not styled, so the editor has no surface of its own", want)
		}
	}
}
