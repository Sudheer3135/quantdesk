# Observer and power checkpoint — review record (2026-10-18)

**Status: proposed for a local-only checkpoint.** No branch, commit, tag or push exists for this record. It describes development work only; nothing here is deployed or active.

## Scope

| Item | Value |
|---|---|
| Baseline commit | `b0a30360b1a787fd9bc99373357168445a1ec80c` |
| Intended branch (not yet created) | `dev/observer-power-local-review` |
| Observer package | 0.4.14 (`scripts/native/observer/VERSION`) |
| Power package | 0.4.2 (`scripts/native/power/VERSION`) |
| Proposed files | **69** = 68 development files (manifest below) + this review record |

The 68 development files are:
- the Observer and power source;
- their READMEs and VERSION files;
- the deployment templates: systemd, nftables, provisioning SQL, Azure templates, configuration examples and the Healthchecks contract;
- `tests/test_observer.py`, `tests/test_power.py` and `tests/test_power_monitoring.py`.

## Not deployed, not active

- No Azure resource, role assignment, schedule, alert, action group or Healthchecks check has been created from these files.
- **Automatic shutdown is BLOCKED.** The power controller must not be installed or enabled on the live VM.
- `LIVE_TRADING` remains false. Nothing in these files places orders or calls the broker.

## Test evidence

| Run | Result |
|---|---|
| Prior independently reviewed run, working checkout, before the address-test change | **1443 passed, 4 skipped** (`test_observer.py`, `test_power.py`, `test_power_monitoring.py`) |
| Pass 25 clean export (`git archive` of the baseline + the 68 files), before the address-test change | **1442 passed, 4 skipped, 1 failed**. The failing test, `test_real_probe_reads_the_checkout_without_importing_the_app`, runs `git rev-parse HEAD`, and the export has no `.git`. It passed when run alone in the real checkout. |
| Pass 26, after the address-test change | `tests/test_observer.py` in full: 783 passed. The Git-dependent test in the real checkout: passed. Ruff on `tests/test_observer.py`: clean. **No full three-suite run was repeated after this change.** |

- **Test-ID baseline:** the 1417 test IDs of the power v0.4.1 baseline are preserved by v0.4.2.
- **Pass 26:**
  - all 1447 IDs collected at pass 24 are still collected;
  - one ID was added: `test_address_disclosure_check_rejects_every_other_ipv4_literal`.
- **Skips:** the 4 skipped tests need Linux, root, systemd or nftables. They are **UNVERIFIED**.

## Manifest history

- **Original reviewed manifest (pass 25, accepted):**
  - 68 files;
  - SHA-256 of the sorted `sha256  path` list: `109cfaf02da8e9668e3eae99a7616e3f004ab9300e149e81add31f06ee6858f1`.
- **Refreshed manifest (below):**
  - the same 68 paths;
  - SHA-256 of the list: `08127235eb13eef4d780f82694a2a869b9626260925b9780fc80a3583912762d`.
- **Only `tests/test_observer.py` differs:** its hash went from `8db0143c5d4badd6a5e496af60ab0f5295aaadd4748ba6dce8b8aeee2ad7b011` to `d9ea2a5d40a65a612eef5a7227696002d43acf09c18b445ecfa714d6f6000b2b`.

### The authorized `tests/test_observer.py` delta

**Removed (1 line):** in `test_example_configuration_loads_and_carries_no_credentials`, an assertion that one literal production address does not appear in the Observer files. The address is deliberately not reproduced here.

**Added (26 lines):**
- `IPV4_LITERAL` and `disallowed_ipv4(text)`. They return every dotted-quad IPv4 literal except:
  - the loopback network `127.0.0.0/8`;
  - the unspecified address `0.0.0.0`.
  These are the only address literals the Observer files need. Octets are compared as numbers, so zero-padded spellings are caught. Non-addresses (an octet above 255) are ignored.
- In the same test: `assert disallowed_ipv4(text) == []` over every Observer file. That rejects the former production address and **any** other public, private, link-local or documentation address.
- A new focused test, `test_address_disclosure_check_rejects_every_other_ipv4_literal`. It covers:
  - **allowed:** loopback, `127.0.0.53`, `0.0.0.0`, a non-address `999.1.2.3`, a time `12.30.00`;
  - **rejected:** a globally routable synthetic address, documentation-range, private and link-local addresses, an address inside a URL, a zero-padded address, and a mixed line that yields only the disallowed literal.
  All synthetic values are unrelated to any production address.

**Not changed:** Observer product source or behaviour, and any other test.

### Refreshed 68-file manifest

```
82368b33c44677e6f6a405e1e21b69696a2c150b282b917c8d9367f7f32ced07  scripts/native/observer/README.md
475c6465c22f0b504215629b5a7c80372f14c0ed114c9b788ed42e95a9c51489  scripts/native/observer/VERSION
8ff4377fdac48b972985b7d27b9553e424f2474717424f2dfc2d1e4980d38402  scripts/native/observer/healthchecks.json.example
f4eced7e46789d1fe5131416e7210ff40f93f9a460c5df1b7a64dbc2d44b1d87  scripts/native/observer/observer.json.example
1b0e341f78d32ed6eb468cec736d209b294ba756d9100c58b67cc42b1e332232  scripts/native/observer/observer.py
02ed5b6117fcdbf8a5be0f5f54ad97949f7c51f37c7114be462b751157946653  scripts/native/observer/operator_acknowledge.py
ccdbd9db0c3e9f788a238bc7acaf345940dda5f80fdabb116f9e3a9013591b71  scripts/native/observer/operator_approval.py
fa7189c74f79cf12c33e4190e1cf87565ea22bfbf25ec7a0dcc4a5778bf0c727  scripts/native/observer/operator_maintenance.py
c36b769f7e7c9d58636734956a80a99126a3df188f274cce91570f5a4b4359ea  scripts/native/observer/quantdesk_observer/__init__.py
82e786393c04678e783ccdaf28b97b42591b23682bd35f65e5ddcc1847bfce95  scripts/native/observer/quantdesk_observer/checks.py
257edad52ff446508285b3ff407f462ed4a0511874492892965b25f049170a61  scripts/native/observer/quantdesk_observer/cli.py
543e50d1387c2df58b753e4f34b6391ff41f2dd311818cb1313d9aee84ab6167  scripts/native/observer/quantdesk_observer/evidence.py
fc13a72c46d8874026eb39cb64b3976dd15e4d8db082bc4e5b5898f127bc3cd6  scripts/native/observer/quantdesk_observer/healthchecks.py
ba4639ecaf7c9443f9f358d5fc3a8363bbe5048906a2602729435e575d9f39a8  scripts/native/observer/quantdesk_observer/probes.py
4a3b48de4d3715033a50a7da615e9adc9c78b3db4708a62f1a65f4e72241226c  scripts/native/observer/quantdesk_observer/store.py
0d2aba748a49d67f87a1ec4fba9791a48951fda93d984b0db4477947e8832090  scripts/native/observer/session_calendar.json
8e05c894e82d7fae12df9c378c5529913e6f1282f5f55f596628580436af2d1a  scripts/native/observer/systemd/quantdesk-observer-afterclose.timer
f929918de23976e81445d929c920d1720d7e69d9d87a01530a5c39bdd93af462  scripts/native/observer/systemd/quantdesk-observer-liveness.timer
4bc1b7d7436b01d851ce08815bc2f624f01471e758f96f2492088f6d660930f1  scripts/native/observer/systemd/quantdesk-observer-midsession.timer
e64408b12c43051db339d566fc2f5d10c272200a0802690d5e8470a2a939694a  scripts/native/observer/systemd/quantdesk-observer-premarket.timer
64602fedaf302d0f52cd730c75ef7608a8a538806b295edbecb07878dac6d0e6  scripts/native/observer/systemd/quantdesk-observer@.service
944c47f4dec3736faccbf2f089a557239ac6e0ef7e71d7498bf62e185df42112  scripts/native/power/README.md
e2f7bb608ebc9e24092ffcb796ad0aa83f0015afe23635f017df52273150f00d  scripts/native/power/VERSION
b2529e3c6dc5b5ff69ed4d6ab312d9f36b22a66fffe996445c96e5c84f0607dd  scripts/native/power/azure/alerts.monitor-missing.proposal.json
4205713920a8a64458f027e07b9bafc62d51ebe8a35ceeaac26f2349be54c3a5  scripts/native/power/azure/alerts.template.json
6797de8bf60743f0a8ce5d2ea65527a00a8e30850f8588c7ea3ef34cb1e913c0  scripts/native/power/azure/qd_monitor.py
68c45c1c42d95bd8aba817d7d322d5683c2e45e97ab3d4d75e8bf65db47101bb  scripts/native/power/azure/qd_start.py
c0318dda959885125352f1521de5f649dff4e6135f9d80b17bab093f584d1739  scripts/native/power/azure/role-automation.json
53a699e92c8fc92243a38515724e8c3a5a4032ac820d677fe52c52550d8ecac7  scripts/native/power/azure/role-vm-deallocate.json
3037d8ee0b428d7474e1006f96caa5a671e3ebde76be02ef7a0858b5bf6f075b  scripts/native/power/azure/schedules.json
ea15a7fdde95cfbf74a06b78d97dc4ab37ffe912769c2e5315bb58783d53b0be  scripts/native/power/azure/special-sessions.example.json
ad91b2430774491f878026077ff7ef3c5bcf7794682ecb5bd7a03279967565eb  scripts/native/power/config/power.json.example
4949cfaef5b98be0a48689af383f372a3829ec156de33596914f101ed8244f81  scripts/native/power/config/power_hold.example.json
1c782fd864123488cba0ecc2cba294518082a1da5cd9c4b71a8f0901b6c3ecfd  scripts/native/power/config/request.json.example
1daa8d9674914a9d2211945093e664861a1015820ceb02b1c660ca32d129b06a  scripts/native/power/config/special_session.example.json
b13678ff64aa5d23bd26eed445c23e076848b722361bc19098b5b1979a3cb594  scripts/native/power/monitoring/healthchecks.json
ec12efe3e7a6a35ff0f0ee7c690e9c7bb407573cf6b3ace23f5ad7943632f28d  scripts/native/power/nftables/quantdesk-imds.nft
eb99c41b5cdefa8a29f175bcd9037b8c97b1da77fe198e38f0819aa6af5e4b09  scripts/native/power/power.py
eb855c00c9b0353325e186dce6d0be784c6689d011bd95350f1cc5973ba592c1  scripts/native/power/provisioning/power-db-role.sql
a1e7a990bf450331e274272de88b0b88c476c1d6524c521d6e3c24768ce4d8d0  scripts/native/power/quantdesk_power/__init__.py
a13c327e67314eb85a93fb32ab54ae6e8b6858ae586ffcef0e63141c1b704cb3  scripts/native/power/quantdesk_power/azure.py
1aadc041dd3e8ab2ea791d97b4f9e8a4fc4224c8834e555c35ebf477717db9bc  scripts/native/power/quantdesk_power/config.py
cf43e382a87847dd1e4bba1b41b7cda88d8dbc98677298f9083c75aaa4676681  scripts/native/power/quantdesk_power/controller.py
40c75c7e9d3f21a62f0bb9deab691c4ccc53926d9170ba77a6604608a911216c  scripts/native/power/quantdesk_power/db.py
333e5f8bbc9a20ecdb1a76e9e488183b2884db483771b7eda0067930b8d3922e  scripts/native/power/quantdesk_power/evidence.py
60a75e5e06a7e4e943e44b21466cc74461877fa78710a96036709b2510f661cb  scripts/native/power/quantdesk_power/facts.py
090965894ddf2a284fa7c7da5dfd6203c2e779e00225631144c2ed1db8fb03ea  scripts/native/power/quantdesk_power/fsafe.py
02802a9a9ea92d9cb27cbb3c9edd994bc1615e9eeb0600207a9922d5c89400a2  scripts/native/power/quantdesk_power/gate.py
f0f9306914fca9d2ccebb455feb72981ab8b0f1d955cb313881d01d5548d4e0e  scripts/native/power/quantdesk_power/journal.py
fdbb1eb52448a5a3eabf76a76925651243d978dfb8d20eaad0c4c16fd6f1ec19  scripts/native/power/quantdesk_power/locks.py
8224ef2608a1c60d3c00be880cc1b00bb3b4aae054031bcee872e4a6cde7491f  scripts/native/power/quantdesk_power/recovery.py
7c0d46409241c77e5b171ebd7be5254c8a6edf8ab182cee94b15fb7c8230ba65  scripts/native/power/quantdesk_power/requests.py
9df2fe889ce39e9c5f8071989a2c587506fd8d32dec9a33b6a39e94c283df19f  scripts/native/power/quantdesk_power/special.py
2c933a3c1bdc1a92c7ea3cbaf90997f2754f4515ff90a9f5f30023d407df7707  scripts/native/power/quantdesk_power/status.py
577ece494a6d6eb3627f7620d2aca91b92d297498391621ee72e0fc91f50570b  scripts/native/power/quantdesk_power/system.py
44a7e337d20e7d0d0bd9a25ae946df39ba3d86bdf448ca76b0d8299508545c13  scripts/native/power/systemd/quantdesk-observer-20-power-isolation.conf
76dfd2992655ad15d2e812885730ad9bbd3b42b1730671bafb46048e282d4760  scripts/native/power/systemd/quantdesk-power-controller.path
d7ff1d4c84b33ddf9717bf42ab180e97b59d03a3fb246441ed7ed308bb2319ac  scripts/native/power/systemd/quantdesk-power-controller.service
9d1c7086eba345f9e19dc3901b9dae3459f17c95fc8583f27af387f7873fcb53  scripts/native/power/systemd/quantdesk-power-imds.service
1d64addb6758f913959494f39b5b2b06455e2487637f812583b2f2c50a921582  scripts/native/power/systemd/quantdesk-power-recover.service
acfd81843fc43ba5e8919223867b10fbed7299019f6f643cf4425c84586a3ff2  scripts/native/power/systemd/quantdesk-power-recover.timer
9b81197722c2d9e397a0bbce246bcf8edc23e2a8e316a9b85147c5166f1e94cf  scripts/native/power/systemd/quantdesk-power-request.service
469112dadeb421d4f50e67b73fd0ef3f8beaba7cfdba66392ae92d37123e72ba  scripts/native/power/systemd/quantdesk-power-request.timer
410670c124ca3bb8fc1a0b289dfd9eb17c7992c97c287abd8329e77872a910ca  scripts/native/power/systemd/quantdesk-power.tmpfiles.conf
064b13545fcaf6cd890280cdf5e79aa8ea5a6c527e6bf2fa0ede067de6072101  scripts/native/power/systemd/quantdesk-workload-10-power-gate.conf
d9ea2a5d40a65a612eef5a7227696002d43acf09c18b445ecfa714d6f6000b2b  tests/test_observer.py
42b77e3d7b61e717fa827fe2affd57118be285c5be8f1433061f3d5311035dd9  tests/test_power.py
a1b908cc1d464cbdf88e3a9b46d25c0e65b84799991d847d5a45921d819c8449  tests/test_power_monitoring.py
```

## Unresolved (not addressed by this checkpoint)

- **Continuous writer exclusion.** Database-enforced writer admission is a proposal only (pass 25, revision 4). Today's exclusion is the cooperative client-side advisory lock.
- **Nondisruptive writer draining.** Bounded work units with explicit completion are designed, not implemented, and not tested on a real PostgreSQL.
- **Strict hold/shutdown ordering.** Holds are read at the pre-checks and at SYSTEM_CLEAR, not between SYSTEM_CLEAR and the deallocation request. The serialized hold authority is a proposal only.
- **Privileged database bypass:** Privileged-role coverage remains unresolved. Assess superuser, ownership, migrator and granted-function capabilities explicitly. pg_write_all_data membership alone does not establish trigger-bypass authority. Continuous writer exclusion remains unverified and shutdown remains blocked.
- **Linux, PostgreSQL and Azure verification:**
  - the 4 platform tests;
  - real systemd and identity output formats;
  - the PostgreSQL behaviours the admission design relies on;
  - `flock`/`fsync` durability on the VM disk;
  - the async-operation read scope;
  - alert firing on zero monitor runs.
  All are **UNVERIFIED**.
- **Independent alert-delivery coverage.** No second alert receiver is named, so a failed action group cannot report itself. Email receipt has not been confirmed.
- **Costs:** not priced, **UNVERIFIED**.
