# Complex Python CLI Requirement

Create a Python command-line program called `text_stats.py`.

Requirements:
- The program must run with `python3 text_stats.py <input-file>`.
- It must read a UTF-8 text file.
- It must print exactly one JSON object to stdout.
- The JSON must contain these keys:
  - `line_count`
  - `word_count`
  - `char_count`
  - `top_words`
- `top_words` must be a list of the 5 most frequent case-insensitive words.
- Each top-word entry must be an object with keys `word` and `count`.
- Ignore punctuation when counting words.
- Add optional support for `--top N` to change how many top words are returned.
- If the input file does not exist, print a JSON error object and exit with code 1.
- Use only the Python standard library.
