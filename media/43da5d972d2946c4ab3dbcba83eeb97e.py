print("How much did you spend today?")
while True:
    try:
        total_expected_price = int(input("Total expected price: "))
        break
    except ValueError:
        print("Please enter a valid number.")

while True:
    try:
        quantity = int(input("How many things did you buy? "))
        break
    except ValueError:
        print("Please enter a valid number.")

sum_price = 0

with open("items.txt", "w") as file:
    for i in range(1, quantity + 1):
        item = input(f"Name of item #{i}: ")

        while True:
            try:
                price_item = int(input(f"Price for {item}: "))
                break
            except ValueError:
                print("Please enter a valid number for the price.")

        sum_price += price_item
        file.write(f"{item}:{price_item}\n")

if sum_price == total_expected_price:
    print("You didn't lie!")
else:
    print(f"You should recalculate your price. Calculated sum: {sum_price}, expected: {total_expected_price}")

with open("items.txt", "a") as file:
    file.write(f"Total Quantity:{quantity}\n")
    file.write(f"Total Price:{sum_price}\n")