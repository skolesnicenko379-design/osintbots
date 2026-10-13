"""Safely merge both sides of a Git conflict in shared_news_history.json."""
import json
import subprocess
from pathlib import Path

from shared_dedup import _clean_recent_posts, canonical_link

HISTORY_PATH = "shared_news_history.json"


def read_index_stage(stage):
    result = subprocess.run(
        ["git", "show", f":{stage}:{HISTORY_PATH}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError(f"{HISTORY_PATH} у Git index має неправильний формат.")
    return data


def main():
    # During a rebase, stage 2 and stage 3 contain the two versions being merged.
    left = read_index_stage(2)
    right = read_index_stage(3)

    merged_posts = []
    for data in (left, right):
        posts = data.get("recent_posts", [])
        if isinstance(posts, list):
            merged_posts.extend(post for post in posts if isinstance(post, dict))

    # Preserve records from both sides, then apply the same 42-hour window
    # and normalization used by the bots.
    recent_posts = _clean_recent_posts(merged_posts)
    links = sorted({
        canonical_link(post.get("link", ""))
        for post in recent_posts
        if canonical_link(post.get("link", ""))
    })

    merged = {"links": links, "recent_posts": recent_posts}
    path = Path(HISTORY_PATH)
    path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Історію об'єднано без втрати записів з обох версій: "
        f"{len(recent_posts)} публікацій у вікні 42 годин."
    )


if __name__ == "__main__":
    main()
