"""Static channel registry for agent-runtime.

Channels are registered statically here rather than via entry_points discovery.
This is intentional for MVP: entry_points discovery would allow any installed pip
package to inject a channel, which is a security concern we do not accept at this stage.

Future: slack, feishu_user_account, cli
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_runtime.channels import ChannelAdapter

# Static mapping of channel name -> module path containing a Channel class.
_STATIC_CHANNELS: dict[str, str] = {
    "feishu": "agent_runtime.channels.feishu.adapter",
}


def load_channel(name: str, config: dict) -> "ChannelAdapter":
    """Load and instantiate a channel adapter by name.

    Args:
        name: Channel name, must be one of the keys in _STATIC_CHANNELS.
        config: Channel-specific configuration dict passed to the adapter constructor.

    Returns:
        An instantiated ChannelAdapter.

    Raises:
        ValueError: If name is not a registered channel.
        ImportError: If the channel module cannot be imported.
        AttributeError: If the channel module has no Channel class.
    """
    if name not in _STATIC_CHANNELS:
        raise ValueError(
            f"unknown channel: {name!r}. allowed: {sorted(_STATIC_CHANNELS)}"
        )

    module = import_module(_STATIC_CHANNELS[name])
    return module.Channel(config)
