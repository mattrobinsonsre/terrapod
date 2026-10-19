// Command terrapod-ee-agent drives ansible inside an execution-environment
// container on the orchestrator's behalf (#2154, implementing #1925).
//
// # Why it exists
//
// A configure runs ansible from the EE's filesystem — its interpreter, its
// ansible-core, its collections — while every decision about the run stays in
// the orchestrator container, which holds the Terrapod code. Containers in a pod
// share a network namespace but not a filesystem, so the orchestrator cannot
// simply exec into the EE: it drives this agent over loopback instead.
//
// The agent is delivered INTO the EE by an initContainer that copies this static
// binary onto a shared volume, and is started with a Kubernetes `command:`
// override which Kubernetes applies over the image's own ENTRYPOINT. That is
// what keeps an EE free of Terrapod code and therefore insensitive to Terrapod
// releases — the EE contributes its filesystem, not its entrypoint.
//
// # The security posture, and why each piece is the way it is
//
//   - LOOPBACK ONLY. The listener binds 127.0.0.1 explicitly. ":7781" would bind
//     every interface including the pod IP, which is routable from the whole
//     cluster. Proven on a real cluster with a positive control: a pod bound to
//     0.0.0.0 was reachable while this one was refused, so it is the bind that
//     denies access rather than any network policy.
//
//   - THE PROBE IS exec, NEVER httpGet. A kubelet httpGet probe connects to the
//     POD IP, which a loopback-only listener cannot answer — so an httpGet probe
//     could only pass by binding every interface, i.e. by breaking the property
//     above. #1925 specified httpGet; that is wrong and this is the correction.
//     The exec probe needs no shell, no curl and no exposed port.
//
//   - A BEARER TOKEN ON EVERYTHING THAT ACTS. Loopback is shared by every
//     container in the pod — including this one, which is where the operator's
//     playbook runs. So the playbook can reach the listener; it must not be able
//     to use it.
//
//   - THE TOKEN IS SCRUBBED FROM THE CHILD'S ENVIRONMENT. A child inherits env,
//     so without this a playbook could read the token out of its own environment
//     and replay it. The same boundary exec_subprocess draws by dropping TP_*
//     before handing an engine its environment.
//
//   - AN ALLOWLIST, NOT ARBITRARY ARGV. The agent runs only the ansible entry
//     points it knows. A generic "exec this" endpoint would be a remote shell,
//     and "it is only on loopback" is not a reason to build one.
package main
