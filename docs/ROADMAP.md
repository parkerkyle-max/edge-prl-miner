# Experimental release milestones

1. **Standalone fixed-model source — initial extraction complete.** The backend
   retains exact numerical/proof checks and correlated pool submit responses.
   Runtime mining does not depend on a remote Windows/WSL model compiler.
   Native artifacts still need to be supplied locally.
2. **Bounded device evidence — initial zero-fee trial complete.** One qualified
   board passed the offline gate and a five-minute pool run with 35 accepted
   replies. See [Validation](VALIDATION.md); this is not long-duration or
   profitability evidence and does not certify later source changes.
3. **Open source with a transparent 1% default fee.** Use MIT for original code,
   retain upstream notices, publish the recipient and time-allocation behavior,
   and support explicit zero-fee operation. Ask users and forks to retain the
   fee voluntarily. Validate scheduler isolation, interruption handling and
   user-work fallback separately from the earlier zero-fee trial. See
   [Developer fee](DEV-FEE.md).
4. **Measure total user value.** Alternate CPU-only and dual windows on the same
   device, repeat them, measure wall power and use a common price/reward snapshot.
   Include CPU output lost to contention, actual fee allocation and downtime.
   A faster NPU path that loses more CPU value does not pass this milestone.
5. **Long-duration stability.** Add a separately reviewed long-run mode after
   bounded tests pass. Target 24–72 hours with job changes, disconnects, cooling,
   memory pressure and restart tests. The current 2400-second limit is not this.
6. **Reproducible installation and wider hardware support.** Document native
   builds, complete the dependency SBOM, distribute only files with established
   redistribution rights, and publish checksums and actual support evidence.
   Each additional NPU/driver/model combination needs its own validation.

One percent of scheduled NPU time is not necessarily one percent of short-term
coin rewards. The MIT license allows users to change or remove the fee. Any
project-income estimate must use active fee-retaining users, actual pool
accounting and collectible output rather than download or accepted-share counts.
