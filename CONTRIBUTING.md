# Contributing to PEACE

Thanks for your interest! Misparsed messages, ideas and pull requests are all welcome.

## Reporting a misparsed message

This is the most useful contribution. Open an
[issue](https://github.com/Skerpsh/Peace/issues/new/choose), pick **Misparsed message**, paste
the text and say what the bot should have understood. For security problems, follow
[SECURITY.md](SECURITY.md) instead.

## Setting up

See [Running it yourself](README.md#running-it-yourself): Python 3.12+ and a bot token.

## Making a change

1. Fork the repository and create a branch from `main`.
2. Keep the change focused: one fix or feature per pull request.
3. Match the style of the code around it. Comments explain *why*, in plain English.
4. Run the tests:

   ```bash
   pytest
   ```

5. Open a pull request describing what changed and why. CI runs the same tests.

## Things to keep in mind

- **Parser changes** come with a case in [`tests/corpus.yaml`](tests/corpus.yaml): the message
  that was misparsed and the expected result. Run the whole corpus: a new keyword often breaks
  another message.
- **All-clears are the dangerous direction.** A change that makes clearing an alert easier needs a
  good reason and a test.
- **Facts, not instructions.** Messages say what is happening, not what people should do.
- **User-facing text is Ukrainian**, code and docs are English.
- **Readable at a glance**: the first line of a message must say where and what.

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
