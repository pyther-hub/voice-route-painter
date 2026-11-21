from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


class LLM:
    def __init__(self, model="gpt-5.4-nano"):
        self.client = OpenAI()  # reads OPENAI_API_KEY from env
        self.model = model

    def ask(self, prompt):
        response = self.client.responses.create(model=self.model, input=prompt)
        return response.output_text


if __name__ == "__main__":
    print(LLM().ask("Say hello in Hinglish in one line."))
