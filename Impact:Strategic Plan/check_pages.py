import pandas as pd
a = pd.read_csv("org_services_run4.csv")
b = pd.read_csv("org_services.csv")
pd.concat([a, b]).to_csv("org_services_sample10.csv", index=False)
print(len(pd.read_csv("org_services_sample10.csv")))