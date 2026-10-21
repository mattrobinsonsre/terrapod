# Ansible Credentials

A [configure operation](runners.md#what-a-configure-actually-runs) runs the
workspace's playbooks against real hosts, so it needs two things no
`terraform`/`native` or `env` variable can carry: the credential ansible
**connects** with, and the Ansible Vault password it **decrypts its own
committed secrets** with.

Both are ordinary Terrapod variables in a credential category — the same
mechanism [private git module sources](module-auth.md) use. You declare the
credential once, as a workspace variable or (better, for a fleet) a variable-set
variable, and Terrapod resolves it server-side and materialises it beside ansible
before the playbook runs.

| Category | Scope (`key`) | Value | Materialized as |
|---|---|---|---|
| `ansible_machine_auth` | a host pattern — the literal `all`, or the name of a group the inventory declares | `{private_key?, password?, user?, become?, become_method?, become_user?, become_password?}` | `0600` key file(s) on the volume shared with the execution environment, plus connection variables injected into the run's rendered inventory at **group** level for that scope |
| `ansible_vault_auth` | the **vault ID label** | `{"password": "…"}` | a `0600` password file, named in the execution environment's `ANSIBLE_VAULT_IDENTITY_LIST` as `label@path` |

Because these are ordinary variables they inherit the whole variable model:
[encryption at rest](encryption-at-rest.md) where the deployment has it enabled,
per-run delivery through the per-run Kubernetes Secret (never the Job spec, never
a log), and **variable sets** — so one machine key or one vault password can be
defined once and assigned to every workspace that needs it.

## Ansible Vault is not OpenBao or HashiCorp Vault

This is the single most important thing on this page, because the two are
completely unrelated and the names collide.

- **Ansible Vault** is a file-encryption feature of ansible itself. It encrypts
  values and files *inside your repository*, and a password decrypts them at
  templating time during a play. `ansible_vault_auth` carries that password, and
  nothing else.
- **OpenBao (or HashiCorp Vault)** is a secret-management server. Terrapod can
  read a variable's value from it at run time — see
  [OpenBao/Vault as a variable value source](vault.md) — which is the
  `value_source: "vault"` mechanism and the `api.config.vault.*` settings.

They are two different features that happen to share a word. An
`ansible_vault_auth` variable needs no OpenBao/Vault server, no
`api.config.vault.enabled`, and no network reach to one; and conversely,
`value_source: "vault"` has nothing to do with Ansible Vault.

The two can be combined usefully — keep the Ansible Vault **password** in
OpenBao/Vault, and have a `value_source: "vault"` variable read it — but not in
the same variable, for the reason below.

## The machine credential: `ansible_machine_auth`

### The envelope

Every field is a string, and an unrecognised field is **refused** at the write
rather than ignored — a credential field under a name nothing reads is a
credential that silently does not apply, and the operator's next signal would be
a configure that cannot connect.

| Field | Becomes | Notes |
|---|---|---|
| `private_key` | `ansible_ssh_private_key_file` | the **path** to a `0600` file Terrapod writes. The key itself never enters the inventory document. A trailing newline is added if you omit one, because ssh otherwise refuses the key with an error naming neither the key nor the missing byte |
| `password` | `ansible_password` | |
| `user` | `ansible_user` | |
| `become` | `ansible_become` | a boolean. `true`/`yes`/`1`/`on` and `false`/`no`/`0`/`off` are accepted; anything else is refused, because a value ansible cannot read as true would read as false and silently drop privilege escalation |
| `become_method` | `ansible_become_method` | |
| `become_user` | `ansible_become_user` | |
| `become_password` | `ansible_become_password` | |

**At least one of `private_key` or `password` is required.** With neither,
ansible has no way to authenticate and the credential does nothing, so it is
refused rather than stored.

### `key` is the scope, and ansible does the scoping

`key` is a host pattern, and there are only two useful things it can be:

- **`all`** — every host in the run. This is ansible's own root group, so the
  credential's variables become the equivalent of `group_vars/all`.
- **the name of a group the inventory declares** — the credential applies to
  that group's hosts, including the ones it holds only through its children,
  because ansible expands group nesting.

Nothing else is accepted. A group name must match the inventory's own
[group-name rule](ansible-inventory.md#group-names); `ungrouped` is refused
specifically, because it is derived from which hosts are in no other group, so
its membership changes whenever any group does — scoping a credential to a set
the operator cannot see or control is not a promise Terrapod will make.

The scoping itself costs Terrapod no logic: the credential's variables are
injected into the run's rendered inventory **at group level** for that scope, and
**ansible's own precedence does the rest**. A per-group key is therefore exactly
as cheap as a fleet-wide one, and several credentials with different scopes
coexist without ordering rules.

### Several keys, one fleet

Declare one entry per scope. A common shape is a fleet-wide fallback plus a
narrower key for the group that needs its own:

```hcl
resource "terrapod_variable" "fleet_key" {
  workspace_id = terrapod_workspace.fleet.id
  key          = "all"
  category     = "ansible_machine_auth"
  sensitive    = true
  value = jsonencode({
    user          = "deploy"
    private_key   = file("${path.module}/keys/fleet")
    become        = "true"
    become_method = "sudo"
  })
}

resource "terrapod_variable" "database_key" {
  workspace_id = terrapod_workspace.fleet.id
  key          = "databases" # a group the inventory declares
  category     = "ansible_machine_auth"
  sensitive    = true
  value = jsonencode({
    user        = "dbadmin"
    private_key = file("${path.module}/keys/databases")
  })
}
```

Hosts in the `databases` group connect as `dbadmin` with that key; every other
host connects as `deploy`. You do not order them.

## The Ansible Vault password: `ansible_vault_auth`

`key` is the **vault ID label** and `value` is `{"password": "…"}`. An empty or
absent password is refused, because without one there is nothing to decrypt with
and the configure would fail part way through a play.

A label must start with a letter, digit or underscore and contain only those plus
`.` and `-`. The reason is mechanical rather than stylistic: the label is
interpolated into the execution environment's `ANSIBLE_VAULT_IDENTITY_LIST`,
whose syntax is `label@file,label@file`, so a label containing `,` or `@` does
not produce a broken label — it produces a **different, silently wrong** list.
That is why it is refused at the write.

### Multiple vault IDs

Declare one variable per label. Terrapod assembles every entry into a single
`ANSIBLE_VAULT_IDENTITY_LIST`, which is how ansible's multiple-vault-ID support
works:

```hcl
resource "terrapod_variable_set_variable" "vault_dev" {
  variable_set_id = terrapod_variable_set.ansible.id
  key             = "dev"
  category        = "ansible_vault_auth"
  sensitive       = true
  value           = jsonencode({ password = var.ansible_vault_dev_password })
}

resource "terrapod_variable_set_variable" "vault_prod" {
  variable_set_id = terrapod_variable_set.ansible.id
  key             = "prod"
  category        = "ansible_vault_auth"
  sensitive       = true
  value           = jsonencode({ password = var.ansible_vault_prod_password })
}
```

A labelled `!vault` block is decrypted by its own identity, and an **unlabelled**
one is tried against each identity in turn — so the single-password case needs no
special handling, and you can label your blocks or not as you prefer.

**A wrong-password run fails loudly.** When nothing in the identity list can
decrypt a block, ansible says so and stops; Terrapod adds no fallback, because
ansible's own behaviour here is already fail-closed.

A variable set is the right home for a vault password, which is why the example
above uses one: the password belongs to the repository's encrypted content rather
than to any one workspace, so every workspace running those playbooks needs the
same one.

## Both categories are always sensitive

The API forces `sensitive = true` on a credential-category variable whatever the
request said, and it **cannot be downgraded** afterwards: a value entered as a
secret does not become world-readable by flipping a flag. The stored value is
never returned by the API.

> **Write `sensitive = true` explicitly in HCL.** The provider's "this category
> is always sensitive" plan modifier currently knows only the two `git_*_auth`
> categories, so an `ansible_*_auth` variable that omits the attribute plans a
> concrete `false` and then applies as `true` — which Terraform reports as
> *"Provider produced inconsistent result after apply"*. Setting it yourself
> makes the plan agree with the result. The examples on this page all do.

## Neither category can take an OpenBao/Vault value source

Setting `value_source: "vault"` on either category is refused with a 422, and the
reason is structural: **a credential in these categories is a JSON object, while
an OpenBao/Vault reference resolves to a single field.** A reference is one
`{mount, path, field}` read producing one string — there is no shape in which it
could deliver `{"user": …, "private_key": …, "become": …}`.

If you want the secret to live in OpenBao/Vault rather than in Terrapod, put the
credential's own secret fields there and reference them from a `static` value —
on the Terraform side, read them with the OpenBao/Vault provider and
`jsonencode` them into the envelope. The same rule, and the same reasoning,
applies to `git_http_auth` and `git_ssh_auth`.

## Where the credential physically goes at run time

The path is the one every other credential takes, with one difference that
matters: ansible runs in a **separate container** from the orchestrator (see
[the Job a configure runs in](runners.md#the-job-a-configure-runs-in)), so the
files have to land somewhere both can see.

1. **Resolution.** The API resolves the workspace's variables and its variable
   sets when a runner claims the run, and puts the two Ansible categories on
   their own attribute of the run's wire — **only for a configure**. A plan or an
   apply is sent an empty list, so these credentials never reach a Job that is
   executing a pull request's Terraform.
2. **Delivery.** The listener writes them as one key of the **per-run Kubernetes
   Secret**, which the Job mounts read-only at
   `/var/run/terrapod/vars/ansible-auth.json`. Values never appear in the Job
   spec, so nothing is readable through `kubectl describe job` or from etcd. The
   Secret carries an `ownerReference` to the Job, so it cascade-deletes with it.
3. **Materialisation.** A runner phase runs **before** the collection install and
   before the playbook, and writes each credential to a `0600` file on the volume
   **shared with the execution environment** — the directory the Job names in
   `TP_EE_CREDENTIALS_DIR`. The file is opened with that mode rather than
   chmod-ed afterwards, so there is no window in which it is readable.
4. **Hand-off.** A machine credential's variables are merged into the inventory
   the phase renders for this run; the vault identity list is handed to the
   execution environment as environment **data**, not exported into the
   orchestrator's own environment.

Three consequences are worth stating plainly:

- **The orchestrator's `/tmp` is not used, deliberately.** It is where every
  other substantial runner tempfile goes, and the execution environment cannot
  see it — a key written there is materialised correctly and invisible to the one
  process that must read it.
- **A machine credential's key material never enters the inventory document.**
  Only the *path* to the file does. And the document the credential is merged
  into is the runner's own local render, not the artifact the API stored — so a
  password delivered this way reaches a file beside ansible and never the stored
  artifact, the workspace's Resolved inventory view, or anything holding
  inventory read.
- **A credential that was declared and cannot be applied fails the run**, naming
  every credential that failed and why, rather than being skipped with a warning.
  A configure that proceeds without its machine credential reports an unreachable
  fleet; one that proceeds without its vault password fails part way through a
  play on a host it has already changed. Both name something other than the
  cause. The *absence* of credentials is not an error — it is the ordinary state
  of a configure against hosts the execution environment can already reach.

## What outranks what

**Host variables in your inventory outrank what a machine credential injects,
and that is the intended order.** The credential's variables enter at group
level; a host variable beats a group variable in ansible. So an operator who
declared `ansible_user` on a host — in a declared
[host variable](ansible-inventory.md#the-seven-structures) or in a committed
`host_vars` file — keeps it, and the credential supplies only what they did not
say.

That is the right way round: their declaration is explicit, specific and visible
in the resolved inventory, while the credential is a default for everything else.
If a credential appears not to be taking effect, this is the first thing to
check.

Our own forced ansible settings outrank anything a credential contributes, for
the same reason they outrank the operator's `ansible.cfg`: a credential must not
be able to re-enable the inventory-plugin dispatcher by supplying a variable of
its own. See
[what a configure actually runs](runners.md#what-a-configure-actually-runs).

## Apply-only, and the phase a credential reaches

Both categories are delivered to a **configure** and to nothing else. Within
that, the [apply-only flag](cloud-identity.md#apply-only-variables) behaves the
way the flag's own reasoning implies: a configure runs the repository's playbooks
against real hosts, so it is a write with apply-equivalent blast radius rather
than a speculative read of a pull request's code — and an apply-only Ansible
credential **is** delivered to it.

So marking one apply-only changes nothing about whether a configure can
authenticate. It is not the thing to look at when one cannot.

## Log safety

Credential handling is log-safe by construction, and the mechanism matters more
than the backstop:

- **The vault password reaches ansible as a file, never on a command line and
  never as the value of an environment variable.** `ANSIBLE_VAULT_IDENTITY_LIST`
  carries `label@path` pairs — labels and paths, not passwords.
- **A machine credential's key material reaches ansible as a file too**, named by
  an inventory variable holding its path.
- Every secret field — a private key, a connection password, a become password,
  a vault password — is additionally **registered with the run-log redactor
  before the file exists**, so there is no window in which ansible could print
  one unredacted. Note this is a backstop rather than the control: a very short
  value is deliberately *not* registered, because a short string matches ordinary
  output and would mangle the log.

What the redactor does **not** cover is every way a playbook can surface a secret
of its own. Raising ansible's verbosity in your committed `ansible.cfg` — which
Terrapod reads and leaves intact — makes it print task arguments, so a role that
passes a decrypted value as a module argument prints that value. That is ansible's
`no_log` to solve, not Terrapod's: the redactor knows the values Terrapod
delivered, not the ones a play derives from them.

A workspace `env` variable cannot change this either way. Those reach the
orchestrator container, not the execution environment — the environment ansible
runs under is the one Terrapod hands it, plus the execution environment's own.

## Troubleshooting

### The configure fails as an unreachable fleet

Check the credential's **scope** first. A machine credential scoped to a group
name is validated for *shape* when you write it, but whether that group exists is
a property of the inventory the run resolved — so renaming or removing a group
leaves a credential pointing at nothing.

Terrapod refuses that rather than running: a configure whose credential applies
to no host would report an unreachable fleet, naming neither the credential nor
the stale scope. The refusal names the scope that does not resolve **and lists
the groups the run's inventory does contain**, so the fix is usually visible in
the message. Rename the credential's `key` to match, or use `all`.

The run's resolved inventory is the authority on which groups exist — read it on
the workspace's inventory view, which answers for the same rows the run resolved
from.

### The credential is configured but ansible is not using it

Look for a **host variable** setting the same thing. Host variables outrank the
credential's group-level injection (see
[what outranks what](#what-outranks-what)), so a declared `ansible_user`, or an
`ansible_user` in a committed `host_vars` file, wins — correctly, but it is not
always what the operator remembered declaring.

The other possibility is the scope: a credential keyed to a group only applies to
that group's hosts, so check the host you are looking at is actually in it.
Ansible expands nesting, so a host that reaches the group only through a child
group *is* covered.

### A write is refused with a 422

The validators name the field and the reason, because "invalid credential" sends
an operator to the wrong place. The cases are:

| What was written | Why it is refused |
|---|---|
| a value that is not a JSON object | both categories carry an envelope, not a literal |
| a field the category does not have | a field nothing reads is a credential that silently does not apply |
| a non-string field value | it cannot be written to a credential file or an inventory variable |
| a machine credential with neither `private_key` nor `password` | ansible would have no way to authenticate |
| a `become` value that is not one of the accepted spellings | a value ansible cannot read as true reads as false, silently dropping escalation |
| a vault credential with an empty or absent `password` | nothing to decrypt with |
| a vault ID label containing `,` or `@` | the label is interpolated into `ANSIBLE_VAULT_IDENTITY_LIST`, so either character produces a different list rather than a rejected one |
| a machine scope that is not `all` or a valid group name | the credential is delivered as inventory variables for that scope, so a name ansible will not resolve to a group applies to nothing |
| `ungrouped` as a machine scope | it is derived, so its membership changes whenever any group does |
| `value_source: "vault"` on either category | an envelope is an object; a reference resolves to one field |

The same validation applies on **every** write path — creating or patching a
workspace variable, and adding or patching a variable-set variable — and it
checks the credential as it will be *after* the write, so a PATCH that changes
only the `key` is caught too.

### Terraform reports "Provider produced inconsistent result after apply"

Add `sensitive = true` to the resource. See
[both categories are always sensitive](#both-categories-are-always-sensitive).

### A vault-encrypted value shows as ciphertext in the resolved inventory

That is correct and is not a configuration error. **Terrapod's inventory
resolution runs with no Ansible Vault password at all** — the credential is
delivered only to a configure Job, and the `ansible-inventory` process the API
runs has every inherited `ANSIBLE_*` variable stripped before its own settings
are applied, so even an `ANSIBLE_VAULT_PASSWORD_FILE` reaching the API pod from
somewhere cannot be picked up.

So an encrypted value in a committed `group_vars` or `host_vars` file resolves as
the `!vault` ciphertext it is stored as. Three things follow, and they are the
reason this is a feature rather than a gap:

- **No Ansible Vault secret ever reaches the API pod**, so none is in the
  resolved inventory artifact, the Resolved view, or anything else inventory read
  can see.
- **Nothing there needs redacting**, because there is no plaintext to redact.
- **Decryption happens where it should** — during the play, in the execution
  environment, with the password materialised as a file for exactly that run.

## See also

- [Running a configure](runners.md#what-a-configure-actually-runs) — the one
  command a configure runs, and the settings it forces
- [Ansible inventory](ansible-inventory.md) — the hosts and groups a scope names,
  and the variable precedence a credential sits inside
- [Private module source authentication](module-auth.md) — the same credential
  mechanism, for private git module sources
- [OpenBao/Vault as a variable value source](vault.md) — the *other* Vault, and
  the one `value_source: "vault"` means
