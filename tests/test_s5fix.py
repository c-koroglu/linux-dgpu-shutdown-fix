# SPDX-License-Identifier: GPL-3.0-or-later

import importlib.machinery
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
# Import the extensionless executable as a module. Its __main__ guard prevents
# command-line parsing from running during the tests.
s5fix = importlib.machinery.SourceFileLoader("s5fix", str(ROOT / "s5fix")).load_module()


DSDT = r'''
DefinitionBlock ("", "DSDT", 2, "VENDOR", "LAPTOP  ", 0x00000002)
{
    External (_SB_.PCI0.GPP0.PEGP, DeviceObj)

    Method (_PTS, 1, NotSerialized)
    {
        OPTS (Arg0)
    }
}
'''

GPU_SSDT = r'''
DefinitionBlock ("", "SSDT", 2, "VENDOR", "GPU     ", 1)
{
    Scope (\_SB.PCI0.GPP0.PEGP)
    {
        Name (DGPS, Zero)
        Name (OPCE, 0x02)

        Method (_PS3, 0, NotSerialized)
        {
            If ((OPCE == 0x03))
            {
                If ((DGPS == Zero))
                {
                    \_SB.PCI0.GPP0.PG00._OFF ()
                    DGPS = One
                }

                OPCE = 0x02
            }
        }
    }
}
'''


def make_adapter(root, address, boot, vendor="0x10de", path=r"\_SB_.PCI0.PEGP"):
    """Create the PCI sysfs files used to describe one test display adapter."""
    device = root / address
    (device / "firmware_node").mkdir(parents=True)
    (device / "power").mkdir()
    (device / "class").write_text("0x030000\n")
    (device / "vendor").write_text(vendor + "\n")
    (device / "device").write_text("0x1234\n")
    (device / "boot_vga").write_text(boot + "\n")
    (device / "firmware_node/path").write_text(path + "\n")
    return device


class S5FixTests(unittest.TestCase):
    def test_gpu_selection_uses_the_non_primary_display_adapter(self):
        """Automatic selection should choose the sole display adapter not used to boot."""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            # Model the small subset of PCI sysfs attributes used by discovery.
            make_adapter(root, "0000:01:00.0", "0", path=r"\_SB_.PCI0.GPP0.PEGP")
            make_adapter(
                root,
                "0000:05:00.0",
                "1",
                vendor="0x1002",
                path=r"\_SB_.PCI0.GP17.VGA_",
            )

            selected = s5fix.choose_gpu(None, root)
            self.assertEqual(selected.address, "0000:01:00.0")
            self.assertEqual(selected.acpi_path, r"\_SB_.PCI0.GPP0.PEGP")

    def test_poweroff_discovery_finds_the_method_and_its_command_latch(self):
        """The analyzer should derive the method and latch without manual ACPI input."""
        strategy = s5fix.find_poweroff_strategy(
            {"DSDT.dsl": DSDT, "SSDT3.dsl": GPU_SSDT}, r"\_SB_.PCI0.GPP0.PEGP"
        )

        # OPCE=3 enables the _OFF branch and the method resets OPCE to 2. That
        # reset is what distinguishes a command latch from a boolean state flag.
        self.assertEqual(strategy["source_table"], "SSDT3")
        self.assertEqual(strategy["method"], r"\_SB.PCI0.GPP0.PEGP._PS3")
        self.assertEqual(
            strategy["trigger"],
            {"object": r"\_SB.PCI0.GPP0.PEGP.OPCE", "value": 3},
        )

    def test_shutdown_risk_uses_live_gpu_and_kernel_evidence(self):
        """Diagnosis should explain a high-confidence result from observable facts."""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            driver = root / "drivers/nvidia"
            driver.mkdir(parents=True)

            # A primary adapter establishes hybrid graphics. The selected
            # adapter has previously slept, may enter D3cold, and uses runtime
            # power management, the combination implicated in the late wake.
            gpu_path = make_adapter(root, "0000:01:00.0", "0")
            make_adapter(root, "0000:05:00.0", "1")
            (gpu_path / "power/control").write_text("auto\n")
            (gpu_path / "power/runtime_suspended_time").write_text("1250\n")
            (gpu_path / "power_state").write_text("D0\n")
            (gpu_path / "d3cold_allowed").write_text("1\n")
            (gpu_path / "driver").symlink_to(driver)

            gpu = s5fix.choose_gpu(None, root)
            evidence = s5fix.assess_shutdown_risk(gpu, root, "7.0.0-test")

            self.assertEqual(evidence["confidence"], "high")
            self.assertEqual(evidence["driver"], "nvidia")
            self.assertTrue(evidence["kernel_has_late_pci_resume"])
            self.assertTrue(evidence["runtime_suspended"])
            self.assertEqual(evidence["current_power_state"], "D0")

    def test_shutdown_risk_reports_incomplete_evidence(self):
        """A compatible-looking GPU must not receive high confidence without sleep history."""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            gpu_path = make_adapter(root, "0000:01:00.0", "0")
            make_adapter(root, "0000:05:00.0", "1")
            (gpu_path / "power/control").write_text("auto\n")
            (gpu_path / "power/runtime_suspended_time").write_text("0\n")
            (gpu_path / "d3cold_allowed").write_text("1\n")

            evidence = s5fix.assess_shutdown_risk(
                s5fix.choose_gpu(None, root), root, "7.0.0-test"
            )

            self.assertEqual(evidence["confidence"], "incomplete")

    def test_conditional_poweroff_without_one_clear_latch_is_rejected(self):
        """The analyzer must stop when it cannot prove how an _OFF branch is enabled."""
        ambiguous = GPU_SSDT.replace("OPCE == 0x03", "DGPS == Zero").replace("OPCE = 0x02", "")

        # Guessing at this condition could alter a state variable rather than
        # enable a command, so unsupported firmware must fail closed.
        with self.assertRaises(s5fix.Failure):
            s5fix.find_poweroff_strategy(
                {"DSDT.dsl": DSDT, "SSDT3.dsl": ambiguous}, r"\_SB_.PCI0.GPP0.PEGP"
            )

    def test_dsdt_patch_targets_only_s5_and_increases_the_revision(self):
        """Generated code should call the discovered method only for complete shutdown."""
        strategy = s5fix.find_poweroff_strategy(
            {"DSDT.dsl": DSDT, "SSDT3.dsl": GPU_SSDT}, r"\_SB_.PCI0.GPP0.PEGP"
        )
        patched = s5fix.patch_dsdt(DSDT, strategy)

        # A newer OEM revision makes Linux prefer the override. The actual
        # correction is guarded by Arg0==5, ACPI's S5 soft-off state.
        self.assertIn("0x00000003", patched)
        self.assertIn("If ((Arg0 == 0x05))", patched)
        self.assertIn(r"Store (0x03, \_SB.PCI0.GPP0.PEGP.OPCE)", patched)
        self.assertIn(r"\_SB.PCI0.GPP0.PEGP._PS3 ()", patched)
        self.assertEqual(patched.count("s5fix:"), 1)
        self.assertFalse(s5fix.has_shutdown_correction(DSDT, strategy))
        self.assertTrue(s5fix.has_shutdown_correction(patched, strategy))

    def test_active_correction_requires_the_poweroff_latch(self):
        """A bare _PS3 call is not a correction when firmware requires a command latch."""
        strategy = s5fix.find_poweroff_strategy(
            {"DSDT.dsl": DSDT, "SSDT3.dsl": GPU_SSDT}, r"\_SB_.PCI0.GPP0.PEGP"
        )
        incomplete = DSDT.replace(
            "        OPTS (Arg0)",
            "        If ((Arg0 == 0x05))\n"
            "        {\n"
            r"            \_SB.PCI0.GPP0.PEGP._PS3 ()" "\n"
            "        }",
        )

        self.assertFalse(s5fix.has_shutdown_correction(incomplete, strategy))

    def test_compiler_cleanup_accepts_only_the_known_external_error(self):
        """iasl error 6163 may remove its generated External line, including a comment."""
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "DSDT.dsl"
            source.write_text(
                "External (_SB_.GOOD, DeviceObj)\n"
                "External (_SB_.TEMP, MethodObj) // 1 Arguments\n"
                "Method (KEEP, 0, NotSerialized) {}\n"
            )
            log = (
                "DSDT.dsl      2: External (_SB_.TEMP, MethodObj) // 1 Arguments\n"
                "Error    6163 - ^ Object is created temporarily in another method\n"
            )

            removed = s5fix.remove_invalid_externals(source, log)
            self.assertEqual(removed, 1)
            self.assertNotIn("TEMP", source.read_text())
            self.assertIn("GOOD", source.read_text())
            self.assertIn("KEEP", source.read_text())

    def test_grub_template_contains_machine_guards_and_valid_shell(self):
        """The rendered generator should guard the payload and remain valid shell code."""
        manifest = {
            "machine": {
                "sys_vendor": "Example Vendor",
                "product_name": "Example Laptop",
                "board_name": "BOARD1",
                "bios_version": "1.2.3",
            },
            "artifacts": {"s5fix-acpi.cpio": "a" * 64},
        }
        text = s5fix.render_grub(manifest)

        # Each identity value must reach the boot-time checks, and every
        # template marker must have been replaced before installation.
        self.assertNotIn("@", text)
        for value in manifest["machine"].values():
            self.assertIn(value, text)
        self.assertIn("Firmware mismatch", text)
        self.assertIn("${GRUB_DISTRIBUTOR:-Linux}", text)
        self.assertIn("grub_quote", text)
        self.assertNotIn("menuentry 'Linux with", text)
        subprocess.run(["sh", "-n"], input=text, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
