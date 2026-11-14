from eth_account import Account
acct = Account.create()
print("Dirección (pública):", acct.address)
print("Clave privada (SECRETA):", acct.key.hex())