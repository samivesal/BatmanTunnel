package app

import (
	"os"
	"runtime"
	"strings"
	"testing"
)

// A build has to be able to name its own successor.
//
// runtime.GOARCH is "arm" for every 32-bit ARM build, whichever variant it was
// compiled for, and the three are not interchangeable: a v7 binary on a v5
// board is an illegal instruction, not a slow one. So the releases name them
// apart, and a running binary that asked for "backpack_linux_arm.tar.gz" would
// be asking for something no release publishes — the update would 404 on every
// ARM machine there is.
func TestAnARMBuildAsksForItsOwnVariant(t *testing.T) {
	saved := GOARM
	t.Cleanup(func() { GOARM = saved })

	if runtime.GOARCH == "arm" {
		for _, v := range []string{"5", "6", "7"} {
			GOARM = v
			if got := AssetArch(); got != "armv"+v {
				t.Errorf("GOARM=%s gave %q, want armv%s", v, got, v)
			}
		}
		// Unstamped — a plain `go build`, which has no published asset of its
		// own. Falling back to the bare GOARCH is right there.
		GOARM = ""
		if got := AssetArch(); got != "arm" {
			t.Errorf("an unstamped arm build gave %q, want arm", got)
		}
		return
	}

	// On every other architecture the stamp is ignored, so a stray value in the
	// build cannot rename the asset.
	GOARM = "7"
	if got := AssetArch(); got != runtime.GOARCH {
		t.Errorf("a %s build with a stray GOARM gave %q", runtime.GOARCH, got)
	}
	if !strings.HasPrefix(AssetName(), "backpack_linux_"+runtime.GOARCH) {
		t.Errorf("AssetName = %q", AssetName())
	}
}

// The release build has to publish an asset for every architecture it builds,
// under exactly the name a binary of that architecture will ask for.
func TestEveryArchitectureBuiltIsAlsoPublished(t *testing.T) {
	mk, err := os.ReadFile("../../Makefile")
	if err != nil {
		t.Skipf("no Makefile here: %v", err)
	}
	src := string(mk)

	for _, want := range []string{
		"ARCHES := amd64 arm64 386 s390x",
		"ARMS   := 5 6 7",
	} {
		if !strings.Contains(src, want) {
			t.Errorf("the release build no longer declares %q", want)
		}
	}
	// The stamp, at the path the linker actually resolves. A wrong symbol path
	// is ignored in silence, so every ARM build would ship unstamped and ask
	// for an asset that does not exist.
	if !strings.Contains(src, "github.com/backpack/backpack/internal/app.GOARM=$$v") {
		t.Error("the ARM builds are not stamped with their variant, so each would " +
			"ask for backpack_linux_arm.tar.gz, which no release publishes")
	}
	// Both loops name the archive the same way the binary will.
	if !strings.Contains(src, "release/backpack_linux_$$a.tar.gz") {
		t.Error("the archives are not named after the architectures that were built")
	}
}

// And the installer can pick the right one for the machine it lands on.
func TestTheInstallerKnowsEveryPublishedArchitecture(t *testing.T) {
	sh, err := os.ReadFile("../../install.sh")
	if err != nil {
		t.Skipf("no install.sh here: %v", err)
	}
	src := string(sh)

	for _, want := range []string{"amd64", "arm64", "386", "s390x", "armv$(arm_variant)"} {
		if !strings.Contains(src, want) {
			t.Errorf("install.sh cannot resolve %q, so that release asset is "+
				"published and unreachable", want)
		}
	}
	// v6 is the safe default when the variant cannot be read: it runs on v6 and
	// v7 both, where a v7 guess on a v6 board does not run at all.
	if !strings.Contains(src, "*)   echo 6 ;;") {
		t.Error("an ARM machine whose variant cannot be determined gets no safe default")
	}
}

// What is built has to be what is published.
//
// The workflow listed the two archives it knew about by name. When the build
// learned five more, nothing failed: they were produced, and then left behind
// on the runner. An update on any of those machines would 404 on an asset the
// release page never carried.
func TestTheWorkflowPublishesEverythingTheBuildProduces(t *testing.T) {
	wf, err := os.ReadFile("../../.github/workflows/release.yml")
	if err != nil {
		t.Skipf("no workflow here: %v", err)
	}
	src := string(wf)
	if !strings.Contains(src, "release/backpack_linux_*.tar.gz") {
		t.Error("the workflow names its assets one by one, so an architecture added " +
			"to the build is published only if somebody remembers to add it here too")
	}
	if !strings.Contains(src, "release/SHA256SUMS") {
		t.Error("the checksums are not published, and the updater refuses an archive " +
			"it cannot verify")
	}
}
