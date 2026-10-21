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
| `ansible_machine_auth` | the credential's **name** — the inventory refers to it as `${terrapod:<name>.<field>}` and decides which hosts get it | `{private_key?, password?, user?, become?, become_method?, become_user?, become_password?}` | a `0640` key file on the volume shared with the execution environment; the placeholder is substituted with its **path**, or with the value for a non-file field |
| `ansible_vault_auth` | the **vault ID label** | `{"password": "…"}` | a `0640` password file, named in the execution environment's `ANSIBLE_VAULT_IDENTITY_LIST` as `label@path` |

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
| `private_key` | `ansible_ssh_private_key_file` | the **path** to a `0640` file Terrapod writes. The key itself never enters the inventory document. A trailing newline is added if you omit one, because ssh otherwise refuses the key with an error naming neither the key nor the missing byte |
| `password` | `ansible_password` | |
| `user` | `ansible_user` | |
| `become` | `ansible_become` | a boolean. `true`/`yes`/`1`/`on` and `false`/`no`/`0`/`off` are accepted; anything else is refused, because a value ansible cannot read as true would read as false and silently drop privilege escalation |
| `become_method` | `ansible_become_method` | |
| `become_user` | `ansible_become_user` | |
| `become_password` | `ansible_become_password` | |

**At least one of `private_key` or `password` is required.** With neither,
ansible has no way to authenticate and the credential does nothing, so it is
refused rather than stored.

### `key` is the credential's name, and the inventory says where it is used

`key` names the credential. Nothing about it decides which hosts it reaches —
that is said in the **inventory**, by a placeholder the runner substitutes while
rendering the inventory for the run:

```
${terrapod:<name>.<field>}
```

`<name>` is the variable's `key`; `<field>` is a field of its envelope. So a
credential named `web-key` carrying a `private_key` is referred to as
`${terrapod:web-key.private_key}`.

**Placement belongs to the inventory because that is already where you say which
host gets what.** Scoping the credential as well would be a second place to say
the same thing, and a second place for the two to disagree. It would also only
ever fill the connection variables Terrapod thought to enumerate — where a
placeholder fills any of them.

Write it wherever you already express a per-host or per-group difference. In an
INI inventory:

```ini
[web:vars]
ansible_ssh_private_key_file=${terrapod:web-key.private_key}

[databases]
db1.example.com ansible_ssh_private_key_file=${terrapod:db-key.private_key}
db2.example.com ansible_ssh_private_key_file=${terrapod:db-key.private_key}
```

…or in a YAML one, or in `group_vars`, or in a declared
[`terrapod_inventory_group_var`](ansible-inventory.md). They are all the same
inventory to Terrapod, and all resolved before substitution happens.

**It can be part of a longer value, which is the point.** A fleet whose per-group
difference is expressed through ssh arguments rather than through
`ansible_ssh_private_key_file` works without Terrapod knowing that variable
exists:

```ini
[legacy:vars]
ansible_ssh_common_args=-o IdentityFile=${terrapod:legacy-key.private_key} -o ProxyJump=bastion
```

#### Why a `${...}` string and not a `!terrapod` tag

A YAML tag would read better — it is how an Ansible Vault value looks — and it
does not work. Measured: **`ansible-inventory` cannot parse an inventory
containing an unknown YAML tag, and it fails silently**, exiting 0 having
resolved zero hosts. `!vault` works only because Ansible registers a constructor
for it in its own loader, and teaching Ansible a Terrapod tag would mean
shipping a plugin into your execution environment.

A `${...}` string is also an ordinary scalar to a generic YAML parser, which a
tag is not — `yaml.safe_load` refuses `!vault` too. So a placeholder leaves your
inventory no less parseable than it already was. It needs no quoting in YAML or
in INI.

#### An unresolvable reference refuses the run

If a placeholder names a credential that was not delivered — a typo in the name,
a variable that does not exist on the workspace or any variable set it uses, or
a field that credential does not carry — the configure refuses before running
anything, naming every reference it could not resolve.

Neither alternative is acceptable. Left in place, Ansible hands ssh the literal
`${terrapod:web-key.private_key}` as a key path; blanked, it connects with no
key at all. Both arrive as an unreachable host, naming neither the credential
nor the typo.

### Several keys, one fleet

Declare one credential per key, and let the inventory place them. A common shape
is a fleet-wide default with a narrower key for the group that needs its own:

```hcl
resource "terrapod_variable" "fleet_key" {
  workspace_id = terrapod_workspace.fleet.id
  key          = "fleet-key"
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
  key          = "db-key"
  category     = "ansible_machine_auth"
  sensitive    = true
  value = jsonencode({
    user        = "dbadmin"
    private_key = file("${path.module}/keys/databases")
  })
}
```

The inventory then decides who gets which — and because these are ordinary
inventory variables, **Ansible's own precedence settles the overlap**: a host
variable beats a group variable, which beats one on `all`.

```ini
[all:vars]
ansible_user=${terrapod:fleet-key.user}
ansible_ssh_private_key_file=${terrapod:fleet-key.private_key}
ansible_become=${terrapod:fleet-key.become}
ansible_become_method=${terrapod:fleet-key.become_method}

[databases:vars]
ansible_user=${terrapod:db-key.user}
ansible_ssh_private_key_file=${terrapod:db-key.private_key}
```

Hosts in `databases` connect as `dbadmin` with that key; every other host
connects as `deploy`. You do not order them, and a single host that needs its
own key is one more line against that host rather than a new kind of thing.

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

> **You may omit `sensitive` in HCL, and the examples set it anyway.** The
> provider plans `sensitive = true` for every credential category, so a config
> that leaves it out plans and applies the same value. Writing it explicitly
> costs nothing and says out loud what the server will do regardless.

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
   before the playbook, and writes each credential to a `0640` file on the volume
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

**Ansible's own precedence decides, because a substituted placeholder is an
ordinary inventory variable.** Terrapod adds no precedence rule of its own, and
that is the whole benefit of placing credentials in the inventory rather than
scoping them: a host variable beats a group variable, which beats one on `all`,
exactly as it does for every other variable you set.

So the overlap resolves the way you would expect:

```ini
[all:vars]
ansible_ssh_private_key_file=${terrapod:fleet-key.private_key}

[databases:vars]
ansible_ssh_private_key_file=${terrapod:db-key.private_key}

[databases]
db1.example.com ansible_ssh_private_key_file=${terrapod:db1-own-key.private_key}
```

`db1` uses its own key, its siblings in `databases` use the group's, and
everything else uses the fleet's. There is nothing to order and no Terrapod rule
to learn.

One consequence worth stating: a value you set **without** a placeholder wins
over one with it at a lower level, for the same reason. A committed
`host_vars/db1.yml` setting `ansible_ssh_private_key_file` to a path of your own
outranks a reference in `[databases:vars]` — which is correct, and is the first
thing to check when a credential appears not to be taking effect.

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

The credential was delivered and ansible did not use it, or used the wrong one.
Where it is used is said in the inventory, so that is where to look.

Read the run's **resolved inventory** on the workspace's inventory view: it
answers for the same rows the run resolved from, and it shows the placeholders
as written. If the variable you expected to carry a reference does not, the
inventory never named the credential for that host.

If the reference is there but the host still cannot be reached, the value it
resolved to is a path on the Job's shared volume, so it is the *credential* that
is wrong rather than its placement — a key for the wrong fleet, a `user` the
host does not have, or escalation the playbook needs and the envelope omits.

A reference naming a credential that was not delivered does **not** reach this
point: the configure refuses before running, and that is
[its own entry](#a-configure-refuses-with-an-unresolvable-credential-reference).

### The credential is configured but ansible is not using it

Look for something else setting the same variable at a higher precedence. These
are ordinary inventory variables once substituted, so **Ansible's own precedence
decides**: a host variable beats a group variable, which beats one on `all`. A
committed `host_vars` file setting `ansible_user` outranks a reference you put
in `[all:vars]` — correctly, but not always what you remembered writing.

The quickest check is the resolved inventory for the host in question, which
shows the value that actually won.

The other possibility is that nothing names the credential at all. A credential
with no reference anywhere is delivered and simply unused — Terrapod does not
warn, because which credentials an inventory uses is the inventory's business
and an unreferenced one is a legitimate thing to have declared ahead of the
inventory that will use it.

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
| a machine scope usable as neither a host name, a group name, nor `all` | the credential is delivered as inventory variables for that scope, so a name ansible cannot resolve applies to nothing. Whitespace and the `--limit` operators `,`&nbsp;`:`&nbsp;`!`&nbsp;`&`&nbsp;`~` are what this excludes |
| `ungrouped` as a machine scope | it is derived, so its membership changes whenever any group does |
| `value_source: "vault"` on either category | an envelope is an object; a reference resolves to one field |

The same validation applies on **every** write path — creating or patching a
workspace variable, and adding or patching a variable-set variable — and it
checks the credential as it will be *after* the write, so a PATCH that changes
only the `key` is caught too.

### A configure refuses with an unresolvable credential reference

The configure refused before running anything, naming every
`${terrapod:<name>.<field>}` in the inventory that it could not resolve.

That is deliberate rather than tolerant. A reference that resolves to nothing
would otherwise be handed to Ansible as a literal — ssh would treat
`${terrapod:web-key.private_key}` as a key path and fail — or, if it were blanked
instead, the host would be contacted with no key at all. Both arrive as an
unreachable host, naming neither the credential nor the typo. The refusal costs
you a run that was going to fail and tells you which string to fix.

Check, in this order:

1. **The name matches.** `<name>` is the variable's `key`, spelled identically in
   both places. `web_key` and `web-key` are different credentials.
2. **The variable is visible to this workspace.** It must be a variable on the
   workspace, or on a variable set the workspace uses.
3. **The field exists on that credential.** `${terrapod:web-key.password}` cannot
   resolve if the envelope carries only `private_key`. The field names are listed
   under [the envelope](#the-envelope).
4. **The category is right.** Only `ansible_machine_auth` fields are referable.
   An Ansible Vault password is delivered through the environment and has nothing
   in the inventory to name — see
   [the Vault password](#the-ansible-vault-password-ansible_vault_auth).

Note that `--limit` is not this: the run's inventory is resolved unlimited, so a
reference on a host the limit excludes still resolves normally. It simply is not
used by that run.

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
- [Ansible inventory](ansible-inventory.md) — the hosts, groups and variables a placeholder is written into,
  and the variable precedence a credential sits inside
- [Private module source authentication](module-auth.md) — the same credential
  mechanism, for private git module sources
- [OpenBao/Vault as a variable value source](vault.md) — the *other* Vault, and
  the one `value_source: "vault"` means
