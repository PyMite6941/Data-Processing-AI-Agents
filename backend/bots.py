from crewai import Agent, Crew, Task, Process, LLM
from crewai_tools import CodeInterpreterTool, SerperDevTool, FileReadTool, DirectoryReadTool, CSVSearchTool,JSONSearchTool, PDFSearchTool, XMLSearchTool, TXTSearchTool, RAGSearchTool, PGSearchTool
import os
import pandas as pd
from pydantic import BaseModel

class Report(BaseModel):
    summary: str
    key_findings: list[str]
    anomalies: list[str]
    recommendations: list[str]

class Bots:
    def __init__(self,context:str):
        self.context = context
        self.smart_llm = LLM(
            model="openrouter/deepseek/deepseek-r1:free",
            api_key=os.getenv('OPENROUTER_API_KEY'),
            tokens=4096,
            temperature=0.1,
            max_retries=2,
            timeout=30
        )

        self.fast_llm = LLM(
            model="openrouter/groq/groq-1:free",
            api_key=os.getenv('OPENROUTER_API_KEY'),
            tokens=4096,
            temperature=0.3,
            max_retries=2,
            timeout=30
        )
        self.file_read = FileReadTool()
        self.directory_read = DirectoryReadTool()
        self.csv_search = CSVSearchTool()
        self.json_search = JSONSearchTool()
        self.pdf_search = PDFSearchTool()
        self.xml_search = XMLSearchTool()
        self.txt_search = TXTSearchTool()
        self.rag_search = RAGSearchTool()
        self.pg_search = PGSearchTool()
        self.serper = SerperDevTool()
        self.code_interpreter = CodeInterpreterTool()

    def create_agents(self):
        self.context_agent = Agent(
            role="Analysis Directive Specialist",
            goal=(
                "Read the user's raw context and rewrite it as a precise, unambiguous "
                "analysis directive. Identify the core question, the most relevant columns "
                "or metrics, and the exact type of analysis needed (trend, comparison, "
                "anomaly, summary, correlation). Output only the directive — nothing else."
            ),
            backstory=(
                "You are an expert at translating vague or freeform requests into sharp, "
                "actionable instructions for data analysts. You have a talent for identifying "
                "what someone actually wants to know versus what they literally said. "
                "You never perform analysis yourself — your only job is to make the analyst's "
                "directive so clear that there is no room for misinterpretation."
            ),
            tools=[],
            verbose=True,
            memory=False,
            llm=self.fast_llm,
            allow_delegation=False,
            cache=True,
        )
        self.prompt_engineer = Agent(
            role="Prompt Engineer",
            goal=(
                "Craft clear, specific prompts to guide the data analyst in performing the "
                "exact analysis needed to answer the user's question. Your prompts should "
                "break down the analysis into logical steps and specify exactly what insights "
                "to look for in the data. Always align your prompts with the directive from the context agent."
            ),
        )
        self.data_analyst = Agent(
            role="Senior Data Analyst",
            goal=(
                "Analyze the provided dataset and extract accurate, relevant insights "
                "that directly answer the user's context. Prioritize patterns, trends, and "
                "anomalies most pertinent to the question asked. Never speculate beyond what the data shows."
            ),
            backstory=(
                "You are a rigorous data analyst with experience across many domains and dataset types. "
                "You always clean data before drawing conclusions, back every finding with statistics, "
                "and present results in plain language scoped precisely to the question you were given. "
                "You do not invent numbers or go beyond the scope of your directive."
            ),
            tools=[self.process_data, self.analyze_data],
            verbose=True,
            memory=True,
            llm=self.smart_llm,
            max_rpm=15,
            allow_delegation=False,
            cache=True
        )
        self.output_formatter = Agent(
            role="Output Formatter",
            goal=()
        )

    def create_tasks(self):
        self.interpret_task = Task(
            description=(
                f"The user has provided this context about what they want analyzed:\n\n"
                f"CONTEXT: {self.context}\n\n"
                "Rewrite this into a structured analysis directive by answering these four questions:\n"
                "1. What is the single core question to answer?\n"
                "2. Which columns or metrics are most relevant to that question?\n"
                "3. What analysis type is needed — trend over time, comparison between groups, "
                "anomaly detection, statistical summary, or correlation?\n"
                "4. Are there any constraints or focus areas implied by the context "
                "(e.g. a date range, a specific user, a threshold)?\n\n"
                "Write the final directive as 3-5 plain sentences addressed directly to a data analyst."
            ),
            expected_output=(
                "A single block of 3-5 plain sentences. No headers, no bullet points, no preamble. "
                "Written as a direct instruction to a data analyst. Must specify: the question to answer, "
                "the relevant columns, the analysis type, and any constraints."
            ),
            agent=self.context_interpreter,
        )
        self.analyze_task = Task(
            description=(
                f"You have been given a dataset and the following analysis directive:\n\n"
                f"DIRECTIVE: {self.context}\n\n"
                "Step 1: Use the process_data tool to clean the dataset and get a readable view.\n"
                "Step 2: Use the analyze_data tool to compute summary statistics.\n"
                "Step 3: Using only what the data shows, answer the directive precisely — "
                "identify relevant trends, patterns, and anomalies."
            ),
            expected_output=(
                "A structured data analysis report containing:\n"
                "1. Data quality summary (rows cleaned, missing values removed)\n"
                "2. Key statistics relevant to the directive (averages, ranges, outliers)\n"
                "3. 3-5 findings that directly answer the directive\n"
                "4. 2-3 actionable recommendations based solely on the data"
            ),
            tools=[self.process_data, self.analyze_data],
            agent=self.data_analyst,
            context=[self.interpret_task],
            output_file="output.log",
            human_input=False
        )

    def create_crew(self,data):
        self.crew = Crew(
            agents=[self.data_analyst],
            tasks=[self.analyze_task],
            process=Process.sequential,
            verbose=True,
            memory=True,
            embedder = {
                "provider": "fastembed",
                "config": {
                    "model": "BAAI/bge-small-en-v1.5",
                }
            }
        )
        self.crew.kickoff(inputs={"data": data})