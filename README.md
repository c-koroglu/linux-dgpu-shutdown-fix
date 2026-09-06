# linux-acpi-dgpu-shutdown-fix

Some laptops appear to shut down normally from Linux but continue producing
heat or consuming substantial power after the screen and fans turn off. On
affected laptops, Linux leaves the dedicated GPU powered during
the final stage of shutdown.

`s5fix` checks whether a laptop matches this known failure pattern. If it does,
the program uses that laptop's own firmware instructions to build a local
correction. No model profile or knowledge of firmware programming is required.

The correction is first offered through a one-time test boot. It does not
rewrite the firmware stored inside the laptop, and it leaves the normal Linux
and Windows boot choices available.

Development and end-to-end testing have so far been performed only on an
Alienware DA15265. Other laptop models remain untested, so their generated
corrections should be evaluated with the one-time test boot before installation.

## The problem

Linux first stops programs and services, then asks hardware drivers to clean up
their devices. Finally, the laptop's firmware places the machine in its off
state.

During normal use, Linux may completely power off an unused dedicated GPU to
save energy. Before calling a PCI driver's final shutdown code, Linux wakes the
device so the driver can still communicate with it. This is intentional. A
driver may need to disable interrupts or prepare the device for `kexec`, which
starts another Linux kernel without restarting the computer.

On an affected laptop, the ordinary shutdown follows this sequence:

1. The unused dedicated GPU is already powered off.
2. Linux powers it back on for the driver's final cleanup.
3. The cleanup finishes, but neither the driver nor the firmware switches the
   GPU's main power off again.
4. The laptop enters its final off state while the GPU continues drawing
   power.

Ordinary shutdown programs have already stopped by this point, so a script or
system service cannot turn the GPU off afterward.

## What `s5fix` changes

Laptop firmware already contains machine-specific instructions for turning the
dedicated GPU on and off. `s5fix` finds the existing power-off instruction and
adds one final call to it after Linux has completed the PCI driver cleanup.

During startup, the firmware supplies hardware instruction tables and Linux
keeps the active copies in memory. The corrected boot entry replaces one of
those in-memory copies with the version built by `s5fix`. Only the copy in
memory is changed. The firmware stored inside the laptop remains untouched.
The replacement applies to that Linux boot only; Windows and the ordinary
Linux entry continue using the original table.

The program also records the laptop model, motherboard, firmware version, and
a digital fingerprint of the correction file. The generated boot entry checks
all of them before loading the correction. If they do not match, it starts
Linux without the correction.

## Requirements

Installation currently supports Ubuntu and related distributions that use
GRUB 2 and `update-grub`.

The build uses the Advanced Configuration and Power Interface (ACPI) toolset
to read and compile firmware tables. It requires:

- Python 3.11 or newer;
- `iasl` from the ACPI Component Architecture (ACPICA) tools;
- GNU `cpio`;
- `tar`.

On Ubuntu and related distributions, install the additional packages with:

```sh
sudo apt install acpica-tools cpio
```

## Quick start

### 1. Diagnose the laptop

Run this as your normal user:

```sh
./s5fix diagnose
```

`diagnose` checks the graphics hardware, Linux power-management state, kernel
behavior, and firmware code. When appropriate, it also builds and verifies a
correction. It may ask for your sudo password once because Linux usually
restricts access to the raw firmware tables.

The first output line summarizes the result:

- **Affected, correction already active:** the laptop has the underlying
  warning signs, but the firmware table loaded for this boot already contains
  an equivalent correction. The current boot is protected.
- **Likely affected and fixable:** the available evidence matches the known
  failure pattern, and a correction was built successfully.
- **Fixable; evidence incomplete:** the firmware can be corrected, but the
  live checks did not provide enough evidence for a high-confidence diagnosis.
- **No build:** the program could not identify a correction it considered safe
  and stopped without installing anything.

### 2. Try the correction for one boot

```sh
sudo ./s5fix try
```

This installs the correction and asks GRUB to select it once. It does not
reboot or shut down the laptop. Reboot when convenient; GRUB will automatically
start `<distro> with ACPI dGPU shutdown fix` for that boot.

After Linux starts, shut it down normally under the conditions that usually
cause the problem, for example with the charger connected. Confirm that the
laptop remains cool and no longer shows abnormal power draw.

The special selection is consumed by that boot. If the corrected boot fails,
force the laptop off and start it again. GRUB will return to its normal default
entry.

### 3. Keep a successful correction

```sh
sudo ./s5fix install
```

This keeps the corrected entry available and initially selects it. It also
makes GRUB remember the last entry chosen from its menu. Choosing Windows or
ordinary Linux makes that choice the default for the following boot; choosing
the corrected entry does the same.

### Removing the correction

```sh
sudo ./s5fix uninstall
```

This removes the files managed by `s5fix` and rebuilds the GRUB menu. None of
the `s5fix` commands reboot or shut down the machine automatically.

## How diagnosis reaches its conclusion

The program does not decide based on the laptop's brand or model. It looks for
several independent pieces of evidence:

1. Linux sees a primary graphics adapter and a separate, non-primary adapter.
2. Automatic power saving is enabled for the non-primary adapter.
3. Linux has successfully suspended that adapter at least once during the
   current boot. The program reads its accumulated suspended time, so the
   adapter does not have to be asleep when `diagnose` is run.
4. Linux permits the adapter to enter `D3cold`, the PCI device state in which
   its main power can be removed.
5. The running kernel version includes the generic PCI
   wake-before-shutdown rule first added in Linux 3.6.8.
6. The firmware provides a clear power-off operation for that graphics
   adapter.
7. The final firmware shutdown path does not already contain the same
   power-off operation.
8. The original and corrected firmware tables compile consistently, and the
   correction passes header, length, checksum, and revision checks.
9. The table currently loaded by Linux is examined separately to determine
   whether an equivalent correction is already active.

Passing all relevant checks gives reasonable confidence that the known problem
exists and that this correction applies. It cannot measure power after the
laptop has turned off, because Linux is no longer running then. The one-time
boot provides the final practical test on the hardware.

## Firmware analysis and build details

The ACPI tables supplied by the laptop describe its hardware and provide small
firmware methods that an operating system can call. `s5fix` captures the
Differentiated System Description Table (DSDT) and the Secondary System
Description Tables (SSDTs), then finds the ACPI device corresponding to the
non-primary graphics adapter.

The analyzer currently accepts a deliberately narrow code structure:

- the graphics device has a `_PS3` (“Power State 3”) method for entering its
  lowest-power state;
- `_PS3` directly calls the firmware operation that switches its power off;
- if that operation requires an enabling value, the analyzer can identify
  exactly one command value that `_PS3` resets afterward.

This is a firmware-code pattern, not a list of supported models. A laptop from
any manufacturer can work if its firmware uses this recognizable structure.
If the code is different or ambiguous, `s5fix` stops instead of guessing at a
hardware-control operation.

For a supported table, the program:

1. decompiles the ACPI Machine Language (AML) into readable ACPI Source
   Language (ASL);
2. recompiles the unchanged DSDT as a control;
3. adds the discovered graphics power-off operation to the complete-shutdown
   state (S5) handled by `_PTS` (“Prepare To Sleep”), the firmware method used
   while preparing to shut down;
4. recompiles the corrected table and compares its compiler diagnostics with
   the unchanged control;
5. validates the table identity, revision, length, and checksum;
6. packages the corrected table for Linux's early-boot ACPI override mechanism;
7. records digital fingerprints of the inputs and generated files in a build
   record called a manifest.

The ACPI specification says `_PTS` should not change a device's current power
state. This project deliberately uses it to issue the final power-off request
after Linux's PCI cleanup. That is an unconventional workaround, which is why
an untested correction is never made persistent automatically. A correction in
the Linux shutdown code itself would be preferable and could eventually make
this workaround unnecessary.

## Less common diagnosis options

If Linux reports more than one possible non-primary graphics adapter,
`diagnose` lists them. Select the intended adapter using its PCI address:

```sh
./s5fix diagnose --gpu 01:00.0
```

Captured firmware tables are kept privately in `build/output/tables/`. They
can be reused when the running table is already corrected:

```sh
./s5fix diagnose --tables build/output/tables
```

Do not commit that directory. It contains manufacturer firmware code and is
excluded by `.gitignore`.

## Command reference

| Command | Effect |
| --- | --- |
| `./s5fix diagnose` | Assesses the machine and builds a correction when appropriate. |
| `sudo ./s5fix try` | Installs the correction and selects it for the next boot only. |
| `sudo ./s5fix install` | Keeps the correction installed and enables GRUB's last-choice behavior. |
| `sudo ./s5fix uninstall` | Removes the installed correction and its GRUB configuration. |

Run `./s5fix COMMAND --help` for command-line help.

## Files and privileged operations

`diagnose` normally runs as your user. If the firmware tables are protected,
its only privileged operation is a `sudo tar` command that reads the named DSDT
and SSDT files from `/sys/firmware/acpi/tables`.

`try`, `install`, and `uninstall` run as root and manage only:

- `/boot/s5fix-acpi.cpio`
- `/etc/grub.d/40_s5fix`
- `/etc/default/grub.d/50-s5fix.cfg`
- `/var/lib/s5fix/manifest.json`

They invoke `update-grub`, `grub-script-check`, and, when appropriate,
`grub-reboot` or `grub-set-default`. They never invoke reboot or power-off.

## Technical terms

- **Firmware:** software supplied by the laptop manufacturer that initializes
  and controls the hardware below the operating-system level.
- **ACPI (Advanced Configuration and Power Interface):** the standard through
  which firmware describes hardware and power controls to the operating
  system.
- **DSDT and SSDT (Differentiated System Description Table and Secondary System
  Description Table):** the main ACPI instruction table and the additional
  tables that extend it.
- **ASL and AML (ACPI Source Language and ACPI Machine Language):** the readable
  and compiled forms of ACPI code.
- **D3cold:** a PCI device power state in which the device's main power has
  been removed.
- **S5:** ACPI's complete software-initiated shutdown state. It is also called
  “soft off” because a small amount of standby circuitry can remain powered.
- **`_PS3`, Power State 3:** an ACPI method that asks one device to enter its
  lowest-power state.
- **`_PTS`, Prepare To Sleep:** an ACPI method called while the computer is
  preparing to sleep or shut down.
- **GRUB:** the boot menu and loader used by this
  project's supported systems.
- **`kexec` (kernel execution):** a Linux mechanism that starts another kernel
  directly, without first restarting the computer through its firmware.
- **ACPICA (ACPI Component Architecture):** the tools used to decompile and
  compile ACPI tables. `iasl` is its compiler and disassembler.

## Technical references

- [Current Linux PCI shutdown code](https://github.com/torvalds/linux/blob/master/drivers/pci/pci-driver.c)
- [Linux 3.6.8 patch adding the pre-shutdown resume](https://lore.kernel.org/lkml/20121122004213.926673842@linuxfoundation.org/)
- [Measured report of the resulting dGPU S5 power drain](https://lkml.iu.edu/2608.1/14248.html)
- [Linux documentation for ACPI table overrides](https://docs.kernel.org/admin-guide/acpi/initrd_table_override.html)
- [ACPI specification: `_PTS`](https://uefi.org/specs/ACPI/6.5/07_Power_and_Performance_Mgmt.html#pts-prepare-to-sleep)

## License

This project is licensed under the GNU General Public License version 3 or,
at your option, any later version. See [LICENSE](LICENSE).
