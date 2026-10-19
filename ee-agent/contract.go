package main

import (
	"fmt"
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
func checkContract() string {
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
