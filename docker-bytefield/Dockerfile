FROM node:20-alpine

# Install Python for background gradient script
RUN apk add --no-cache python3

# Install bytefield-svg
RUN npm install -g bytefield-svg

# Copy background gradient script and wrapper
COPY add_svg_background.py /usr/local/bin/add_svg_background.py
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/add_svg_background.py /usr/local/bin/entrypoint.sh

WORKDIR /diagrams

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["--help"]
