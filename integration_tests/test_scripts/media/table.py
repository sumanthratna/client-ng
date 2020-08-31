import wandb
import pandas as pd


def gen_table(n=1):
    data = [["I love my phone", n, n], ["My phone sucks", n, n]]
    return wandb.Table(data=data, columns=[
                       "Text", "Predicted Label", "True Label"])


def gen_df_table():
    df = pd.DataFrame(columns=["Foo", "Bar"], data=[
                      ["So", "Cool"], ["&", "Rad"]])
    return wandb.Table(dataframe=df)


all_tests = {
    "table_single": gen_table(1),
    "table_dataframe_single": gen_df_table()
    # Seqs of tables is currently unsupported
    # "table_dataframe_many": [gen_df_table(), gen_df_table()],
    # "table_many": [gen_table(), gen_table()],
}

if __name__ == "__main__":
    wandb.init(project="test-table")
    wandb.log(all_tests)
