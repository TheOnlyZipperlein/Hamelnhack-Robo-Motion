import pygame
import sys


class main:

    def __init__(self):
        print("hello")

        self.mode = pygame.display.set_mode((500,500))
        self.bg = pygame.image.load("")
        

        pygame.display.set_caption("Emulator")


    def run(self):

        while True:
            
            self.mode.fill((255,255,0))



            for event  in pygame.event.get():
                if event.type == pygame.QUIT:
                    pygame.quit()
                    sys.exit()

            pygame.display.flip()


app = main()
app.run()