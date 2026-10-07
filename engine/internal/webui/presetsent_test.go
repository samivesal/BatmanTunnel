package webui

import (
	"io/fs"
	"regexp"
	"strings"
	"testing"
)

// The preset the Add form shows is the preset it sends.
//
// One preset button carries `on` in the markup, so the form opens with Balance
// visibly selected. The choice was only recorded when somebody pressed a
// button, though — so accepting what was already on screen sent no preset at
// all, the tunnel was written without one, and the edit dialog later read back
// an empty value and fell to whichever option the preview happened to be drawn
// with. The two screens disagreed about a tunnel, and neither was wrong:
// nothing had been recorded either way.
func TestTheAddFormSendsThePresetItIsShowing(t *testing.T) {
	loadPanel()

	js, err := fs.ReadFile(panelRoot, "js/views/add.js")
	if err != nil {
		t.Fatalf("add.js: %v", err)
	}
	src := string(js)

	if !strings.Contains(src, "markedPreset") {
		t.Fatal("the form does not derive its preset from the button it is showing, " +
			"so accepting the default sends nothing")
	}
	// Derived from the markup, not written in.
	if !regexp.MustCompile(`markedPreset\s*=\s*\(\)\s*=>`).MatchString(src) {
		t.Error("markedPreset is not a function reading the DOM")
	}
	if !strings.Contains(src, "chosen.preset = markedPreset()") {
		t.Error("the chosen preset is not kept in step with the marked button, so a " +
			"preset that disappears with the transport leaves the payload behind")
	}

	// And the markup holds up its half: every choice names its value, and
	// exactly one per group is marked.
	html, err := fs.ReadFile(panelRoot, "views/add.html")
	if err != nil {
		t.Fatalf("add.html: %v", err)
	}
	buttons := regexp.MustCompile(`<button[^>]*class="rp[^"]*"[^>]*>`).FindAllString(string(html), -1)
	if len(buttons) == 0 {
		t.Fatal("the form offers no presets at all")
	}
	on := 0
	for _, b := range buttons {
		if !strings.Contains(b, "data-pre=") {
			t.Errorf("a preset button names no value: %s", b)
		}
		if strings.Contains(b, `class="rp on"`) {
			on++
		}
	}
	if on == 0 {
		t.Error("no preset is marked as chosen, so the form opens showing none")
	}
}
