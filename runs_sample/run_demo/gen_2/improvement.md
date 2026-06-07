> **[SAMPLE PLACEHOLDER — not the Feedback-Agent's real output]**

# Generation 2: Schema linking

Gen 1 dumped the entire schema for every database. The feedback agent observed many `no-such-column`/`no-such-table` errors and added a **schema-linking** step: select only the tables/columns relevant to the question before generating SQL.
