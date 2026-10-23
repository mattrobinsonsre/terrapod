package main

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// contractBinaries is what an execution environment must provide. Derived from
// what the orchestrator actually invokes rather than from a wish list:
//
//	ansible-playbook  runs the configure
//	ansible-galaxy    installs collections and roles, in the EE because the core
//	                  that installs content must be the core that runs it
//	ssh               reaches a managed node
//	git               clones a git-sourced role, which ansible-galaxy does itself
var contractBinaries = []string{"ansible-playbook", "ansible-galaxy", "ssh", "git"}

// checkContract reports what an EE is missing, as a human sentence, or "" when
// it conforms.
//
// This runs for OPERATOR-SUPPLIED images as much as for ours: ours assert the
// same contract at build time, but an image named in the Helm label→URI map has
// never been through that. Checking it here turns a non-conforming image from a
// mid-run mystery into a startup failure that names what is absent.
//
// `ansible --version` is part of it, not decoration. An EE can have every binary
// on PATH and still be unable to run ansible as the uid we start it with: with
// an unwritable HOME, `DEFAULT_LOCAL_TMP` resolves to `/.ansible/tmp` and ansible
// dies inside `import ansible.constants`; with a writable HOME but no passwd
// entry for the uid, a task dies `KeyError: getpwuid(): uid not found`. Both were
// measured, and both are invisible to a `command -v` check.
// requiredUID is the uid an execution environment must run as (#1928).
//
// The orchestrator and the EE exchange files on the shared volume -- the
// callback plugin one way, its event stream the other -- and pinning both to one
// uid is what lets those be `0600` rather than group-readable. The Job spec
// sets `runAsUser` on the sidecar, so a CONFORMING image is started as this uid
// whatever its own `USER` says; what fails is an image that cannot function as
// it, which is the case this catches at the probe rather than mid-playbook on an
// unreadable file.
//
// A narrowing of the contract, and therefore a breaking change for an
// operator-supplied image built against the older one -- recorded in
// docs/upgrading-to-2.0.md.
const requiredUID = 1000

// checkContract reads this process's own uid. The uid is a PARAMETER of
// checkContractAs rather than read inside it, so the tests can drive both
// branches: read in place, a wrong uid short-circuits every binary assertion
// and the rest of the contract becomes untestable anywhere but on a uid-1000
// machine -- the environment dependence this file exists to turn into a legible
// refusal, reintroduced into its own tests.
func checkContract() string {
	return checkContractAs(os.Getuid())
}

func checkContractAs(uid int) string {
	if uid != requiredUID {
		return fmt.Sprintf(
			"this execution environment is running as uid %d; an EE must run as uid %d "+
				"so that it and the orchestrator can exchange files on the shared volume "+
				"without granting them to a group (the Job sets runAsUser, so an image "+
				"that cannot run as this uid is the case that reaches here)",
			uid, requiredUID,
		)
	}

	var missing []string
	for _, bin := range contractBinaries {
		if _, err := exec.LookPath(bin); err != nil {
			missing = append(missing, bin)
		}
	}
	if len(missing) > 0 {
		return fmt.Sprintf(
			"this execution environment is missing %s on PATH; an EE must provide %s",
			strings.Join(missing, ", "), strings.Join(contractBinaries, ", "),
		)
	}

	out, err := exec.Command("ansible", "--version").CombinedOutput()
	if err != nil {
		return fmt.Sprintf(
			"ansible is on PATH but will not run as this uid: %v. Output: %s "+
				"(an EE needs a writable HOME and a passwd entry for the uid it runs as)",
			err, strings.TrimSpace(string(out)),
		)
	}
	return ""
}
