# a00z_lpg_no_timeout_overlay: applies no-timeout + policy patches at load time.
import logging
from nixe.helpers.overlay_utils.no_timeout_patch import apply_all_patches as _apply_no_timeout
from nixe.helpers.overlay_utils.lp_policy_patch import apply_policy_patch as _apply_policy

log = logging.getLogger(__name__)

async def setup(bot):
    try:
        _apply_no_timeout()
        _apply_policy()
        log.info("[nixe-overlay] a00z overlays applied: no-timeout + lp-policy.")
    except Exception as e:
        log.warning("[nixe-overlay] overlay apply error: %s", e)
