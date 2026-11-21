from .command_errors import command_errors
from .commit_hygiene import commit_hygiene
from .lockfile import lockfile_edits
from .toolchain import toolchain_churn
from .context import prompt_context
from .edit_churn import edit_churn
from .editing import blind_edits, dependency_drift, edit_rejections, generated_edits, site_edit_mismatch, stuck_loops
from .environment import environment_thrash
from .feedback_loops import correction_loops, feedback_loops
from .global_patterns import global_patterns
from .hotspots import context_blasts, hotspots, orientation
from .permission_denials import permission_denials
from .recovery import failure_loops, noop_retries, recovery_loops
from .repeated_reads import repeated_reads
from .safety import destructive_commands
from .testing import selector_misses, workaround_scripts
from .validation import false_completions, low_validation, no_reproduction, premature_completion, regressions_after_green


__all__ = [
    "blind_edits",
    "command_errors",
    "commit_hygiene",
    "lockfile_edits",
    "toolchain_churn",
    "context_blasts",
    "correction_loops",
    "destructive_commands",
    "edit_churn",
    "edit_rejections",
    "dependency_drift",
    "generated_edits",
    "environment_thrash",
    "failure_loops",
    "false_completions",
    "feedback_loops",
    "global_patterns",
    "hotspots",
    "low_validation",
    "no_reproduction",
    "noop_retries",
    "orientation",
    "permission_denials",
    "premature_completion",
    "prompt_context",
    "recovery_loops",
    "repeated_reads",
    "selector_misses",
    "site_edit_mismatch",
    "regressions_after_green",
    "",
    "stuck_loops",
    "workaround_scripts",
]
