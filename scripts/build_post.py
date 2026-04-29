from datetime import datetime
import json
import frontmatter
import pathlib
import argparse
import jinja2
import os

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/articles/")
    parser.add_argument("--dst", default="content/posts/")
    args = parser.parse_args()

    src_path = pathlib.Path(args.src)
    dst_path = pathlib.Path(args.dst)
    dst_path.mkdir(parents=True, exist_ok=True)

    template_file = pathlib.Path("layouts/_templates/post.md.j2")
    if not template_file.exists():
        print("Template not found")
        return

    template = jinja2.Template(template_file.read_text(encoding="utf-8"))

    for f in src_path.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))

            # Determine if cheapest
            prices = [p.get("price", 999999) for p in data.get("products", []) if isinstance(p.get("price"), (int, float))]
            min_p = min(prices) if prices else None
            for p in data.get("products", []):
                if min_p and p.get("price") == min_p:
                    p["is_cheapest"] = True
                else:
                    p["is_cheapest"] = False

            md_body = template.render(**data)

            post = frontmatter.Post(md_body, **{
                "title": data.get("title", "No Title"),
                "date": data.get("date", datetime.now().isoformat()) if "datetime" in globals() else data.get("date", "2026-04-29"),
                "tags": data.get("tags", []),
                "draft": False,
                "slug": data.get("slug")
            })

            out_file = dst_path / f"{data.get('slug', f.stem)}.md"
            out_file.write_text(frontmatter.dumps(post), encoding="utf-8")
            print(f"Rendered: {out_file}")
        except Exception as e:
            print(f"Error processing {f}: {e}")

if __name__ == "__main__":
    main()
