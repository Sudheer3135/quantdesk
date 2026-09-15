FROM node:20-alpine
WORKDIR /app

# The lockfile is copied with the manifest and `npm ci` installs exactly what
# it pins. `npm install` against package.json alone resolved fresh versions
# inside the image, so the container could run different code from the host
# the tests had passed on — and an added dependency was simply absent until
# someone remembered to rebuild, which on 31-Aug took the chart down with
# "Failed to resolve import lightweight-charts".
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev"]
