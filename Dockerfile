# Python ka ek halka-phulka official version use karo
FROM python:3.10-slim

# Kaam karne ke liye /app naam ka folder banao
WORKDIR /app

# Code copy karne se pehle, saari libraries install kar lo
# Isse Docker build fast hota hai
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ab baaki ka saara project code copy karo
COPY . .

# start.sh ko run karne ki permission do
RUN chmod +x start.sh

# Jab container start ho, toh yeh command chalao
CMD ["bash", "start.sh"]
