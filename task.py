# task.py
import time
import random

def time_dec(function):
    #fail if job takes longer than 20 seconds to complete
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = function(*args, **kwargs)
        end_time = time.time()
        execution_time = end_time - start_time
        print(f"Execution time: {round(execution_time, 2)} seconds")
        if execution_time > 20:
            raise TimeoutError("Function execution exceeded 20 seconds")
        return result
    return wrapper
3
phrases = ["hi", "hello", "hey", "greetings", "salutations", "howdy", "yo", "sup", "what's up", "good day"]

@time_dec
def process_phrases():
    for i, phrase in enumerate(phrases, start=1):
        print(f"STEP {i}: {phrase}")
        time.sleep(random.uniform(0.5, 3))  # Sleep for a random time between 0.5 and 3 seconds

process_phrases()