from dataclasses import dataclass

TAGS = {"soundcloud": "SC", "podcast": "PC"}  # the two-char mark in front of a non-YouTube row; YouTube rows have none


@dataclass
class Video:
    id: str
    title: str
    uploader: str
    duration: int | None  # seconds; None for live streams or when yt-dlp does not know
    source: str = "youtube"  # one of youtube.SOURCES for a search result; the site's name for a link
    link: str | None = None  # the page URL yt-dlp reported, for every source but youtube; a podcast's enclosure URL
    thumbnail: str | None = None  # the cover image's URL, utils.thumbnail_url() of the yt-dlp entry

    @property
    def url(self):
        if self.source == "youtube":
            return f"https://www.youtube.com/watch?v={self.id}"
        return self.link

    @property
    def tagged_title(self):
        """The title after its source's tag, "SC Song", so mixed lists read; a YouTube title as it is."""
        tag = TAGS.get(self.source)
        return f"{tag} {self.title}" if tag else self.title
