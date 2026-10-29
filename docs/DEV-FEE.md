# Developer fee: 1%, visible and optional

Edge PRL's default configuration allocates **1% of scheduled NPU mining time**
to development. Set `"dev_fee": 1` to retain the default, or `"dev_fee": 0` to
turn it off. This configuration field is an integer percentage; `0.01` is not a
valid substitute for `1`.

If you benefit from the project, please keep the default enabled. We ask fork
maintainers to retain the developer-fee setting and project attribution as well.
This is voluntary support: the MIT license permits modifications, including
removing the fee. There is no anti-tamper requirement or additional license
condition requiring a donation.

## Recipient and pool

The developer's public native PRL address is:

```text
prl1pwjessemugthkad9hkttcx2rvenux2q0w0xcah2j0a7zfrycjhz5qhn7apw
```

Developer work uses worker `edge-devfee`. The address is intentionally public.
The miner needs no wallet private key, seed phrase, wallet database or ability to
spend any user's funds.

This release enables fee work only when the user has explicitly selected the
supported Suprnova plaintext TCP pool on port 3370. It uses that same selected
pool endpoint with the developer's public account. A Kryptex configuration must
use `"dev_fee": 0`; the configuration check refuses `1`. There is no hidden switch
from Kryptex TLS to an unrelated plaintext pool.

## Schedule and limits

For a fee-enabled foreground run, the guard schedules repeating 600-second
cycles:

| Segment | Nominal interval | Mining account |
| --- | --- | --- |
| User | First 594 seconds | User's configured public account/address |
| Developer | Last 6 seconds | Public developer address above |

Only one owned native mining child runs at a time. The guard stops that child at
a role boundary before launching a fresh child for the next role. Each child has
its own pool connection and pending-submit state. User and developer roles are
identified separately in events and status; developer accepted replies must not
be presented as user accepted replies.

The developer's six-second window includes child startup, model preparation,
network connection and work. It is not extended to compensate for overhead or a
missing share. A short run that ends before its first developer window assigns
no developer time. Missed fee windows, disconnections and downtime create no
fee debt and are not recovered with larger later windows. If a running developer
child exits early, the guard returns to user work; the remaining fee window is
not retried to make up a charge. A guard or process-launch error can stop the
experiment. Cooling, memory, stop and overall runtime guards remain in force.

The maximum foreground run is currently 2400 seconds. There is no persistent
fee accumulator, background fee service or boot task. Setting `"dev_fee": 0`
disables fee scheduling completely. The user's original configuration and fresh
offline validation gate remain the basis for the run; changing configuration or
code requires a new gate.

## What 1% does and does not measure

The percentage describes nominal scheduled time in complete cycles. It is not
1% of wallet balance, settled revenue, accepted-share count or external CPU-miner
work. Pool luck, difficulty, short sessions and startup costs can produce a
realized payout ratio very different from 1%, including no developer shares.
Restarting children also costs user mining time beyond the nominal developer
window; include that overhead when measuring total user value.

The fee does not modify a separate XMR or other CPU miner. NPU preparation can
still contend for CPU resources, so compare total CPU+NPU output, not NPU speed
alone. The economics helper accepts a fee fraction for its conditional estimate;
that fraction is a different unit from the miner's integer-percent setting.

## Evidence status

The earlier five-minute board test and 35 accepted replies were obtained with a
**0% fee** before this scheduler was added. They are not evidence that fee work
has produced accepted shares or a payment. The new scheduler is an experimental
source implementation; its offline tests do not establish hardware timing,
real-pool fee acceptance, settled developer income or long-term reliability.
A fresh, separately recorded board trial is required before claiming those
properties. See [Validation](VALIDATION.md).
