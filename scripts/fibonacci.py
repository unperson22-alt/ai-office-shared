```python
def fibonacci(n):
    # Обработка базовых случаев
    if n < 0:
        raise ValueError("n должно быть неотрицательным")
    if n <= 1:
        return n
    
    # Итеративный подход для эффективности
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b


# Примеры использования
if __name__ == "__main__":
    # Вывод первых 10 чисел Фибоначчи
    for i in range(10):
        print(f"F({i}) = {fibonacci(i)}")
```