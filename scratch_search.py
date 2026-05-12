import csv

with open('Invoice Details Report_Extract.csv', 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        if '12612980' in str(row) or '3424' in str(row):
            print(row)
