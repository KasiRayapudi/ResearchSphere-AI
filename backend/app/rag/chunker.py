class RecursiveTextChunker:
    """
    Splits text recursively into chunks with controlled overlap.
    Matches enterprise RAG chunking standards.
    """

    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 200):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split_text(self, text: str) -> list[str]:
        if not text:
            return []

        chunks = []
        start = 0
        text_length = len(text)

        while start < text_length:
            end = min(start + self.chunk_size, text_length)

            # Find natural boundary
            if end < text_length:
                last_space = text.rfind("\n", start, end)
                if last_space == -1:
                    last_space = text.rfind(" ", start, end)
                if last_space != -1 and last_space > start:
                    end = last_space

            chunks.append(text[start:end].strip())
            start = end - self.chunk_overlap if end < text_length else text_length

        return [c for c in chunks if c]
