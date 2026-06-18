import json

# Example: Creating and working with JSON data

# Create a Python dictionary
data = {
    "name": "John Doe",
    "age": 30,
    "city": "Paris",
    "skills": ["Python", "JavaScript", "SQL"],
    "is_active": True
}

# Convert Python dictionary to JSON string
json_string = json.dumps(data, indent=2)
print("JSON String:")
print(json_string)

# Convert JSON string back to Python dictionary
parsed_data = json.loads(json_string)
print("\nParsed Data:")
print(parsed_data)

# Write JSON to a file
with open("data.json", "w") as file:
    json.dump(data, file, indent=2)
print("\nData written to data.json")

# Read JSON from a file
with open("data.json", "r") as file:
    loaded_data = json.load(file)
print("Data loaded from file:")
print(loaded_data)
