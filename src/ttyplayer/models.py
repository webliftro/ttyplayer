from dataclasses import dataclass


@dataclass
class Video:
    id: str
    title: str
    uploader: str
    duration: int | None  # seconds; None for live streams or when yt-dlp does not know

    @property
    def url(self):
        return f"https://www.youtube.com/watch?v={self.id}"
