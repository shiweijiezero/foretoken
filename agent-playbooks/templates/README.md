# Experiment Records and Templates

English | [简体中文](README_zh.md)

The [record layout](experiments.md) shows where experiment notes and run evidence belong. Refer to these templates when writing notes under `results/`:

- [Experiment notes](experiment-template.md): the overall goal, scope, comparison method, and findings across iterations.
- [Iteration notes](iteration-template.md): analysis, design, changes, measurements, and decisions for one approach.

After each perf/eval command finishes, the developer or agent edits `results/<goal>/<motivation>/iterations/<name>/notes/iteration.md`: add the run link, explain what was tested and learned, and record failures or questions that need another measurement. Append to the same note for additional runs of that approach.

When the iteration ends, update `results/<goal>/<motivation>/notes/experiment.md` with its conclusion, a link to the iteration note, and the next direction. Commands save execution evidence and create blank notes; they do not write these explanations. See [completion steps](experiments.md#at-the-end-of-each-iteration) for decisions and resource cleanup.
