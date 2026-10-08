from dataclasses import asdict, dataclass


@dataclass
class Message:
    platform: str
    role: str
    channel: str
    user: str
    text: str
    ts: str
    thread: str = ""
    is_dm: bool = False
    is_mention: bool = False
    command: str = ""
    user_name: str = ""
    is_bot: bool = False
    connection: str = ""

    @property
    def key(self):
        return f"{self.platform}:{self.role}:{self.channel}:{self.ts}"

    def as_dict(self):
        return asdict(self)
