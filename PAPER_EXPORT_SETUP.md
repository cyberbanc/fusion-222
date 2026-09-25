# Full PAPER backtest archive

The `TILDA_FUSION_222.html` / `.txt` T123 block includes a button for a ZIP of
all available PAPER decisions, round histories, and model snapshots.

The endpoint `/exports/paper-history.zip` is publicly accessible without a
password after deploying the **fusion-222** PAPER service. Anyone who knows its
URL can download the archive, regardless of whether they know the Tilda site.
There is no `PAPER_EXPORT_PASSWORD` Railway variable to set.

Deploy the PAPER backend changes, then update the
actual Tilda T123 block with the current version of `TILDA_FUSION_222.html`.
The repository copy may differ from the text currently published in Tilda;
merge the new button into the current T123 block if it has diverged.

The endpoint reads only an explicit allowlist of PAPER tables. `fusion222_real_*`
and state/configuration tables are excluded. All tables are read from one
repeatable-read transaction. `manifest.json` in the ZIP lists exported tables,
row counts, and the UTC export timestamp. Other CSV buttons and the PAPER worker
are unchanged. Large exports consume CPU and database I/O while the ZIP is built;
run outside the betting decision window if latency is a concern.
