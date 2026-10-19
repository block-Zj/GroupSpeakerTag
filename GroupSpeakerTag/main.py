from __future__ import annotations

from langbot_plugin.api.definition.plugin import BasePlugin


class GroupSpeakerTagPlugin(BasePlugin):
    """给群消息打上发言人标记，并把 QQ 图片在本地转成 base64。"""

    async def initialize(self) -> None:
        pass

    def __del__(self) -> None:
        pass
