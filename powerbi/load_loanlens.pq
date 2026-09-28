// Power Query (M) - load every LoanLens export from a folder.
// In Power BI Desktop: Home > Transform data > New Source > Blank Query > Advanced Editor,
// paste this, set DataFolder to the absolute path of powerbi/data, then right-click each
// table in the "Tables" result and choose "Add as New Query".
let
    DataFolder = "C:\\path\\to\\LoanLens\\powerbi\\data",
    Files = Folder.Files(DataFolder),
    Parquet = Table.SelectRows(Files, each [Extension] = ".parquet"),
    Tables = Table.AddColumn(Parquet, "Data", each Parquet.Document([Content])),
    Result = Table.SelectColumns(
        Table.TransformColumns(Tables, {{"Name", each Text.BeforeDelimiter(_, ".parquet")}}),
        {"Name", "Data"})
in
    Result
