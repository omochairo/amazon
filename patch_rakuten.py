import re
with open('scripts/fetch_rakuten.py', 'r') as f:
    content = f.read()

# I see what happened. Look at line 58:
# json.dump({"keyword": args.keyword, "items": items}, f, ...)
# Wait, no, the user's log said: NameError: name 'search_kw' is not defined on line 31.
# My CURRENT file has search_kw defined on line 30, and used on line 34.
# How could it be undefined on line 31 in the user's run?
# Because the user's run WAS USING THE OLD FILE!
