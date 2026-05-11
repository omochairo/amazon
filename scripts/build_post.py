import json
import frontmatter
import pathlib
import argparse
import jinja2
from datetime import datetime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/articles/")
    parser.add_argument("--dst", default="hugo/content/posts/")
    args = parser.parse_args()

    src_path = pathlib.Path(args.src)
    dst_path = pathlib.Path(args.dst)
    dst_path.mkdir(parents=True, exist_ok=True)

    template_file = pathlib.Path("scripts/templates/post.md.j2")
    if not template_file.exists():
        print("Template not found")
        return

    template = jinja2.Template(template_file.read_text(encoding="utf-8"))

    for f in src_path.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))

            md_body = template.render(**data)

            slug = data.get("slug", f.stem)
            post = frontmatter.Post(md_body, **{
                "title": data.get("title", "No Title"),
                "date": data.get("date", datetime.now().isoformat()),
                "tags": data.get("tags", []),
                "draft": False,
                "slug": slug,
            })

            out_file = dst_path / f"{slug}.md"
            out_file.write_text(frontmatter.dumps(post), encoding="utf-8")
            print(f"Rendered: {out_file}")
        except Exception as e:
            print(f"Error processing {f}: {e}")


if __name__ == "__main__":
    main()
