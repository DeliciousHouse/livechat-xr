"""Exercise the workflow's compiler setup without installing machine software."""
import pathlib
import shutil
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 required")
class InstallerCompiler(unittest.TestCase):
    def test_compiler_pin(self):
        pwsh = shutil.which("pwsh")
        assert pwsh is not None
        workflow = (ROOT / ".github/workflows/build.yml").read_text()
        # Run the actual setup block, stopping before the real compiler invocation.
        setup = workflow.split("          $v =", 1)[1].split("\n", 1)[1]
        setup = setup.split("\n          & ", 1)[0]
        for existing, installed, exit_code, succeeds in (
            ("missing", "6.7.1.0", 0, True),
            ("6.6.1.0", "6.7.1.0", 0, True),
            ("6.8.0.0", "6.7.1.0", 0, True),
            ("6.7.1.0", "6.7.1.0", 0, True),
            ("missing", "6.7.1.0", 1, False),
        ):
            with self.subTest(existing=existing, installed=installed, exit_code=exit_code):
                script = """
$ErrorActionPreference = 'Stop'
$global:compilerVersion = '%s'
function choco {
    if ($args -notcontains '--version' -or $args -notcontains '6.7.1' -or
        $args -notcontains '--force' -or $args -notcontains '--allow-downgrade') {
        throw 'Install must enforce the pin, including upgrades and downgrades'
    }
    $global:compilerVersion = '%s'
    $global:LASTEXITCODE = %s
}
%s
Write-Output "COMPILER=$global:compilerVersion"
""" % (existing, installed, exit_code, setup)
                result = subprocess.run(
                    [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
                    capture_output=True, text=True, timeout=30,
                )
                if succeeds:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("COMPILER=6.7.1.0", result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertNotIn("COMPILER=", result.stdout)

    def test_installer_rejects_other_compiler_versions(self):
        installer = (ROOT / "installer/livechat-xr.iss").read_text()
        self.assertIn("#if Ver != 0x06070100\n  #error Expected Inno Setup 6.7.1\n#endif", installer)
